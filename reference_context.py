"""Scoped QQ reference aliases and explicitly labelled history evidence."""
import json
import time

def reference_fields(payload):
    scene = payload.get('message_scene')
    ext = scene.get('ext') if isinstance(scene, dict) else None
    fields = {}
    for entry in ext if isinstance(ext, list) else []:
        if isinstance(entry, str) and '=' in entry:
            key, value = entry.split('=', 1)
            if key in ('msg_idx', 'ref_msg_idx') and value:
                fields[key] = value
    return fields


def cache_reference(path, scope, payload, sent=None, history_fallback=False):
    """Resolve exact aliases, with optional separately labelled author evidence."""
    import sqlite3
    from contextlib import closing
    import json
    import time
    now = time.time()
    fields = reference_fields(payload)
    with closing(sqlite3.connect(str(path), timeout=2)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS refs(scope TEXT, alias TEXT, record TEXT, created REAL, PRIMARY KEY(scope,alias))')
        db.execute('DELETE FROM refs WHERE created < ?', (now - 30 * 86400,))
        resolved = dict(payload)
        direct = payload.get('message_reference')
        direct = direct.get('message_id') if isinstance(direct, dict) else None
        reference = fields.get('ref_msg_idx') or (direct if isinstance(direct, str) else None)
        if reference:
            row = db.execute('SELECT record FROM refs WHERE scope=? AND alias=?', (scope, reference)).fetchone()
            if row and json.loads(row[0]).get('id'):
                record = json.loads(row[0])
                resolved['message_reference'] = {'message_id': record['id']}
                resolved['msg_elements'] = [record]
            else:
                # A ref index establishes that the user quoted an older message,
                # but it is not the gateway message ID. Never emit it as reply ID.
                resolved['_qq_reference_unresolved'] = True
                if not row and history_fallback:
                    resolved['_qq_reference_history'] = history_author_candidate(db, scope, payload, reference, now)
        if sent is None:
            author = payload.get('author')
            author = {k: v for k, v in author.items() if k in ('nickname', 'nick', 'username', 'member_openid', 'user_openid', 'openid', 'id') and isinstance(v, str)} if isinstance(author, dict) else {}
            record = {'id': payload.get('id'), 'author': author, 'content': payload.get('content') or ''}
            aliases = [record['id'], fields.get('msg_idx')]
        else:
            record, aliases = sent
        if isinstance(record.get('id'), str) and isinstance(record.get('author'), dict):
            encoded = json.dumps(record, ensure_ascii=False)
            for alias in aliases:
                if isinstance(alias, str) and alias:
                    # A conflicting alias must never silently change author.
                    existing = db.execute('SELECT record FROM refs WHERE scope=? AND alias=?', (scope, alias)).fetchone()
                    if existing and json.loads(existing[0]).get('id') != record['id']:
                        db.execute('UPDATE refs SET record=? WHERE scope=? AND alias=?', ('{}', scope, alias))
                        continue
                    db.execute('INSERT OR IGNORE INTO refs VALUES(?,?,?,?)', (scope, alias, encoded, now))
        db.execute('DELETE FROM refs WHERE rowid IN (SELECT rowid FROM refs ORDER BY created DESC LIMIT -1 OFFSET 100000)')
        db.commit()
    return resolved

def history_author_candidate(db, scope, payload, reference, now=None):
    elements = payload.get('msg_elements')
    if not reference or not isinstance(elements, list):
        return {'status': 'unavailable'}
    selected = [e for e in elements if isinstance(e, dict) and e.get('msg_idx') == reference]
    if len(selected) != 1 or not isinstance(selected[0].get('content'), str):
        return {'status': 'unavailable'}
    text = selected[0]['content'].replace('\r\n', '\n').strip()
    if not text or '=== 消息 ' in text or '[关联消息]' in text:
        return {'status': 'unsupported_content'}
    timestamp = time.time() if now is None else now
    rows = db.execute('SELECT DISTINCT record FROM refs WHERE scope=? AND created>=? AND created<=? LIMIT 20001', (scope, timestamp - 30 * 86400, timestamp)).fetchall()
    if len(rows) > 20000:
        return {'status': 'too_many_records'}
    seen = set()
    authors = {}
    count = 0
    for row in rows:
        record = json.loads(row[0])
        if not isinstance(record, dict) or not isinstance(record.get('content'), str):
            continue
        if record['content'].replace('\r\n', '\n').strip() != text:
            continue
        mid = record.get('id')
        if not isinstance(mid, str) or mid == payload.get('id') or mid in seen:
            continue
        seen.add(mid)
        author = record.get('author')
        if not isinstance(author, dict):
            return {'status': 'missing_identity'}
        identity = next((author.get(k) for k in ('member_openid', 'user_openid', 'openid', 'id') if isinstance(author.get(k), str) and author[k]), None)
        if not identity:
            return {'status': 'missing_identity'}
        authors[identity] = author
        count += 1
    if len(authors) > 1:
        return {'status': 'ambiguous_authors', 'author_count': len(authors)}
    if not authors:
        return {'status': 'not_found'}
    # No message_id: duplicate text by one author does not identify a message.
    return {'status': 'history_author_candidate', 'author': next(iter(authors.values())), 'matching_message_count': count, 'platform_confirmed': False}


def reference_scope(account_id, event, payload):
    group = event in {"GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"}
    author = payload.get("author") or {}
    target = payload.get("group_openid") if group else next((author.get(k) for k in ("user_openid", "openid", "id") if author.get(k)), None)
    if not account_id or not target:
        raise ValueError("Missing reference cache scope")
    return json.dumps([account_id, "group" if group else "private", target])


def attach_reference_context(message, payload):
    elements = payload.get("msg_elements")
    elements = elements if isinstance(elements, list) else []
    context = "\n".join(e["content"] for e in elements if isinstance(e, dict) and isinstance(e.get("content"), str))
    direct = payload.get("message_reference")
    target = direct.get("message_id") if isinstance(direct, dict) else None
    target = target.strip() if isinstance(target, str) else ""
    fields = reference_fields(payload)
    quoted = bool(target or fields.get("ref_msg_idx"))
    if not quoted and (not context or context.strip() == str(payload.get("content") or "").strip()):
        return
    author = None
    if target:
        message["raw_message"].insert(0, {"type": "reply", "data": target})
        author = next((e.get("author") for e in elements if isinstance(e, dict) and (e.get("id") == target or e.get("message_id") == target)), None)
    evidence = payload.get("_qq_reference_history") or {}
    if author:
        heading = "用户引用的历史消息；作者：" + _author_name(author)
    elif evidence.get("status") == "history_author_candidate":
        heading = "用户引用的历史消息；候选作者：" + _author_name(evidence["author"]) + "；依据：本会话完整原文历史匹配，非平台引用索引确认"
    elif evidence.get("status") == "ambiguous_authors":
        heading = "用户引用的历史消息；完整原文匹配到多个作者，无法唯一确认"
    elif quoted:
        heading = "用户引用的历史消息；作者暂无法确认"
    else:
        heading = "QQ平台附带上下文；未提供明确引用关系或作者"
    addition = "\n[" + heading + "]\n" + (context or "[平台未提供引用正文]") + "\n[历史上下文结束]"
    message["raw_message"].append({"type": "text", "data": addition})
    message["processed_plain_text"] += addition


def _author_name(author):
    name = next((author.get(k) for k in ("nickname", "nick", "username", "member_openid", "user_openid", "openid", "id") if isinstance(author.get(k), str) and author[k]), "未知")
    return json.dumps(name, ensure_ascii=False)

