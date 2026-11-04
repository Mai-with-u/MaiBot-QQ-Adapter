"""QQ 官方群聊管理 OpenAPI 封装与对外能力组件。

覆盖官方「群聊管理」下的全部接口：群基本信息、机器人群内状态、群禁言状态与成员禁言
（含解除禁言）、入群申请列表与审批、入群自动审批策略，以及群成员与群黑名单管理。

这些接口大多要求机器人在目标群内具备管理员身份，且部分接口仍处于内邀阶段：
平台会以 ``err_code=11282``（检查管理员未通过）或 ``11253``（应用无接口访问权限）拒绝调用，
错误会按统一格式抛给调用方，不做静默兜底。

组件以 ``@API(public=True)`` 暴露，其它插件的调用方式：

    result = await ctx.api.call("adapter.group.info", group_openid="群号或群 OpenID")
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

from maibot_sdk import API

if TYPE_CHECKING:
    from .plugin import QQOfficialAdapterPlugin

# 官方对单次请求的成员数量上限，超限时按分片提交。
MUTE_MEMBERS_PER_REQUEST = 20
REMOVE_MEMBERS_PER_REQUEST = 20
BLACKLIST_MEMBERS_PER_REQUEST = 20
STRATEGY_GROUP_LIMIT = 100
STRATEGY_WHITELIST_PER_REQUEST = 10000
# 官方列表接口的单页上限。
JOIN_REQUEST_PAGE_MAX = 50
MEMBER_PAGE_SIZE = 30
BLACKLIST_PAGE_MAX = 100
STRATEGY_PAGE_MAX = 50
# 禁言接口的 op 取值：add 增加禁言、update 更新到期时间、del 解除禁言。
MUTE_OPS = frozenset({"add", "update", "del"})
# 入群审批与黑名单接口的 op 取值。
APPROVAL_OPS = frozenset({"approve", "decline"})
BLACKLIST_OPS = frozenset({"add", "del"})


def _chunk(items: Sequence[Any], size: int) -> List[List[Any]]:
    """按官方单次上限切分批量参数。"""
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


class GroupAdminClient:
    """群聊管理 OpenAPI 客户端：只做协议拼装、分片与错误包装，不持有插件状态。"""

    def __init__(self, plugin: "QQOfficialAdapterPlugin") -> None:
        self._plugin = plugin

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        session = self._plugin._require_session()
        token = await self._plugin._authorize()
        request_kwargs: Dict[str, Any] = {"headers": {"Authorization": f"QQBot {token}"}}
        # 空串与 None 表示不传该 query 参数（首次拉取列表时 cursor 可不传）。
        if params:
            request_kwargs["params"] = {key: value for key, value in params.items() if value not in (None, "")}
        if json_body is not None:
            request_kwargs["json"] = dict(json_body)
        async with session.request(method, self._plugin._api_url(path), **request_kwargs) as response:
            text = await response.text()
            payload: Any = {}
            if text.strip():
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
            # 删除类接口返回 204 空响应；其余接口必须返回 JSON 对象。
            if response.status >= 400 or not isinstance(payload, Mapping):
                raise RuntimeError(
                    f"QQ 群聊管理接口 {method} {path} 失败: HTTP {response.status} {self._describe_error(payload, text)}"
                )
            return dict(payload)

    @staticmethod
    def _describe_error(payload: Any, text: str) -> str:
        """提取官方错误码、说明与 trace_id，便于调用方区分权限问题与参数问题。"""
        if isinstance(payload, Mapping):
            code = payload.get("err_code", payload.get("code"))
            detail = f"err_code={code} message={payload.get('message')}"
            if payload.get("trace_id"):
                detail = f"{detail} trace_id={payload['trace_id']}"
            return detail
        return text[:200]

    async def get_group_info(self, group_openid: str) -> Dict[str, Any]:
        return await self._request("GET", f"/v2/groups/{quote(group_openid, safe='')}/info")

    async def get_bot_state(self, group_openid: str) -> Dict[str, Any]:
        return await self._request("GET", f"/v2/groups/{quote(group_openid, safe='')}/bot_state")

    async def get_mute_state(self, group_openid: str) -> Dict[str, Any]:
        return await self._request("GET", f"/v2/groups/{quote(group_openid, safe='')}/restrict_chat_setting")

    async def set_member_mute(self, group_openid: str, members: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """设置、更新到期时间或解除成员禁言；超过单次上限时自动分片。"""
        path = f"/v2/groups/{quote(group_openid, safe='')}/restrict_chat_setting"
        chunks = _chunk(list(members), MUTE_MEMBERS_PER_REQUEST)
        for chunk in chunks:
            await self._request("POST", path, json_body={"members": chunk})
        return {"submitted_members": sum(len(chunk) for chunk in chunks)}

    async def list_members(self, group_openid: str, cursor: str = "") -> Dict[str, Any]:
        return await self._request(
            "GET", f"/v2/groups/{quote(group_openid, safe='')}/members", params={"cursor": cursor}
        )

    async def get_member(self, group_openid: str, member_openid: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/v2/groups/{quote(group_openid, safe='')}/members/{quote(member_openid, safe='')}",
        )

    async def remove_members(
        self, group_openid: str, member_openids: Sequence[str], add_to_blacklist: bool = False
    ) -> Dict[str, Any]:
        """批量移出群成员；add_to_blacklist 为真时同时拉黑。"""
        path = f"/v2/groups/{quote(group_openid, safe='')}/batch_remove_members"
        chunks = _chunk(list(member_openids), REMOVE_MEMBERS_PER_REQUEST)
        fail_openids: List[str] = []
        for chunk in chunks:
            payload = await self._request(
                "POST", path, json_body={"member_openids": chunk, "add_to_member_blacklist": add_to_blacklist}
            )
            fail_openids.extend(str(item) for item in payload.get("add_to_member_blacklist_fail_openids") or [])
        return {"removed_members": sum(len(chunk) for chunk in chunks), "blacklist_fail_openids": fail_openids}

    async def list_blacklist(self, group_openid: str, cursor: str = "", limit: int = 0) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/v2/groups/{quote(group_openid, safe='')}/member_blacklist",
            params={"cursor": cursor, "limit": limit or None},
        )

    async def update_blacklist(self, group_openid: str, op: str, member_openids: Sequence[str]) -> Dict[str, Any]:
        """加入或移出群黑名单；目标用户在群中时无法加入黑名单。"""
        path = f"/v2/groups/{quote(group_openid, safe='')}/member_blacklist"
        chunks = _chunk(list(member_openids), BLACKLIST_MEMBERS_PER_REQUEST)
        fail_openids: List[str] = []
        for chunk in chunks:
            payload = await self._request("POST", path, json_body={"op": op, "member_openids": chunk})
            fail_openids.extend(str(item) for item in payload.get("fail_openids") or [])
        return {"op": op, "submitted_members": sum(len(chunk) for chunk in chunks), "fail_openids": fail_openids}

    async def list_join_requests(self, group_openid: str, cursor: str = "", limit: int = 0) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"/v2/groups/{quote(group_openid, safe='')}/join_request_list",
            params={"cursor": cursor, "limit": limit or None},
        )

    async def approve_join_request(
        self,
        group_openid: str,
        member_openid: str,
        op: str,
        join_request_id: str = "",
        reject_reason: str = "",
        add_to_blacklist: bool = False,
    ) -> Dict[str, Any]:
        """审批入群申请；join_request_id 建议回传，避免申请列表变化后误批。"""
        body: Dict[str, Any] = {"op": op, "add_to_member_blacklist": add_to_blacklist}
        if join_request_id:
            body["join_request_id"] = join_request_id
        if op == "decline" and reject_reason:
            body["reject_reason"] = reject_reason
        await self._request(
            "POST",
            f"/v2/groups/{quote(group_openid, safe='')}/approval_join_request/{quote(member_openid, safe='')}",
            json_body=body,
        )
        return {"op": op, "member_openid": member_openid}

    async def create_strategy(
        self,
        group_openids: Sequence[str] = (),
        group_ids: Sequence[int] = (),
        is_enable: str = "",
        expire_at: str = "",
        remark: str = "",
    ) -> Dict[str, Any]:
        """创建入群自动审批策略；group_openids 与 group_ids 二选一必填。"""
        body: Dict[str, Any] = {}
        if group_openids:
            body["group_openids"] = list(group_openids)
        if group_ids:
            body["group_ids"] = list(group_ids)
        if is_enable:
            body["is_enable"] = is_enable
        if expire_at:
            body["expire_at"] = expire_at
        if remark:
            body["remark"] = remark
        return await self._request("POST", "/v2/groups/join_approval_strategy", json_body=body)

    async def list_strategies(self, cursor: str = "", limit: int = 0) -> Dict[str, Any]:
        return await self._request(
            "GET", "/v2/groups/join_approval_strategy", params={"cursor": cursor, "limit": limit or None}
        )

    async def update_strategy(
        self,
        strategy_id: str,
        *,
        is_enable: str = "",
        expire_at: str = "",
        group_action: Optional[Mapping[str, Any]] = None,
        remark: str = "",
    ) -> Dict[str, Any]:
        """修改策略配置或关联群；群标识形式需与创建策略时一致。"""
        body: Dict[str, Any] = {}
        if is_enable:
            body["is_enable"] = is_enable
        if expire_at:
            body["expire_at"] = expire_at
        if group_action:
            body["group_action"] = dict(group_action)
        if remark:
            body["remark"] = remark
        return await self._request(
            "PATCH", f"/v2/groups/join_approval_strategy/{quote(strategy_id, safe='')}", json_body=body
        )

    async def delete_strategy(self, strategy_id: str) -> Dict[str, Any]:
        await self._request("DELETE", f"/v2/groups/join_approval_strategy/{quote(strategy_id, safe='')}")
        return {"deleted_strategy_id": strategy_id}

    async def update_strategy_whitelist(self, strategy_id: str, op: str, whitelist_users: Sequence[str]) -> Dict[str, Any]:
        """增删策略白名单；单策略上限 10 万，单次请求上限 1 万。"""
        path = f"/v2/groups/join_approval_strategy/{quote(strategy_id, safe='')}/whitelist_users"
        chunks = _chunk(list(whitelist_users), STRATEGY_WHITELIST_PER_REQUEST)
        latest: Dict[str, Any] = {}
        for chunk in chunks:
            latest = await self._request("POST", path, json_body={"op": op, "whitelist_users": chunk})
        return {"op": op, "submitted_users": sum(len(chunk) for chunk in chunks), **latest}

    async def execute_strategy(self, strategy_id: str) -> Dict[str, Any]:
        """立即执行策略：官方异步全量扫描关联群，约 10 分钟完成。"""
        await self._request("POST", f"/v2/groups/join_approval_strategy/{quote(strategy_id, safe='')}/execute")
        return {"executed_strategy_id": strategy_id}


class GroupAdminMixin:
    """群聊管理能力组件：把官方群聊管理接口以公开 API 暴露给其它插件。

    ``group_openid`` 参数同时接受群 OpenID 与已绑定的统一群号；``member_openids`` 中的
    元素同样接受成员 OpenID 与已绑定的统一 QQ 号。未绑定的纯数字账号无法反查，
    会以“无法解析 OpenID”报错，与禁言/撤回工具的处理保持一致。
    """

    async def _invoke_group_api(self, label: str, call: Callable[[], Awaitable[Mapping[str, Any]]]) -> Dict[str, Any]:
        """统一执行群聊管理调用：成功回传官方响应，失败回传可读错误而不抛出。"""
        try:
            payload = await call()
        except Exception as exc:
            self.ctx.logger.warning("QQ 群聊管理接口失败（%s）: %s", label, exc)
            return {"success": False, "error": str(exc)}
        return {"success": True, "data": dict(payload)}

    def _require_group_openid(self, group_openid: str) -> str:
        normalized = str(group_openid or "").strip()
        if not normalized:
            raise ValueError("group_openid 必须非空，可使用群号或群 OpenID")
        return self._resolve_group_openid(normalized)

    def _require_member_openids(self, member_openids: Optional[Sequence[Any]]) -> List[str]:
        resolved: List[str] = []
        for item in member_openids or []:
            normalized = str(item or "").strip()
            if not normalized:
                continue
            openid = self._resolve_user_openid(normalized)
            if not openid or openid.isdigit():
                raise ValueError(f"无法解析成员 {normalized} 的 OpenID，需其消息经本适配器接收或先 /assign_id 绑定")
            resolved.append(openid)
        if not resolved:
            raise ValueError("member_openids 不能为空")
        return resolved

    def _build_mute_entries(self, members: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
        """规范禁言请求项：op=add/update 需要 RFC3339 到期时间，op=del 表示解除禁言。"""
        entries: List[Dict[str, Any]] = []
        for item in members or []:
            if not isinstance(item, Mapping):
                raise ValueError("members 的每一项都必须是对象")
            member_openid = self._require_member_openids([item.get("member_openid")])[0]
            op = str(item.get("op") or "add").strip().lower()
            if op not in MUTE_OPS:
                raise ValueError("members[].op 必须是 add、update 或 del")
            entry: Dict[str, Any] = {"op": op, "member_openid": member_openid}
            if op == "del":
                # 传空串表示立即解除禁言。
                entry["mute_expire_at"] = str(item.get("mute_expire_at") or "")
            else:
                expire_at = str(item.get("mute_expire_at") or "").strip()
                if not expire_at:
                    raise ValueError("op=add/update 时必须提供 mute_expire_at（RFC3339）")
                entry["mute_expire_at"] = expire_at
            entries.append(entry)
        if not entries:
            raise ValueError("members 不能为空")
        return entries

    @API("adapter.group.info", description="获取 QQ 群基本信息（群名、简介、分类、标签、人数）", version="1", public=True)
    async def api_group_info(self, group_openid: str = "") -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        return await self._invoke_group_api("获取群基本信息", lambda: self._group_api.get_group_info(group))

    @API("adapter.group.bot_state", description="获取机器人在群内的状态（身份、入群时间、是否允许主动推送）", version="1", public=True)
    async def api_group_bot_state(self, group_openid: str = "") -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        return await self._invoke_group_api("获取机器人群内状态", lambda: self._group_api.get_bot_state(group))

    @API("adapter.group.mute.get", description="查询群禁言状态（全体禁言规则与当前禁言中的成员）", version="1", public=True)
    async def api_group_mute_state(self, group_openid: str = "") -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        return await self._invoke_group_api("查询群禁言状态", lambda: self._group_api.get_mute_state(group))

    @API(
        "adapter.group.mute.set",
        description="设置群成员禁言：op=add 禁言、op=update 更新到期时间、op=del 解除禁言（mute_expire_at 传空串）",
        version="1",
        public=True,
    )
    async def api_group_mute_set(
        self, group_openid: str = "", members: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        entries = self._build_mute_entries(members)
        return await self._invoke_group_api("设置群成员禁言", lambda: self._group_api.set_member_mute(group, entries))

    @API("adapter.group.mute.clear", description="解除群成员的禁言（restrict_chat_setting 的 op=del）", version="1", public=True)
    async def api_group_mute_clear(
        self, group_openid: str = "", member_openids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        members = [
            {"op": "del", "member_openid": openid, "mute_expire_at": ""}
            for openid in self._require_member_openids(member_openids)
        ]
        return await self._invoke_group_api("解除群成员禁言", lambda: self._group_api.set_member_mute(group, members))

    @API("adapter.group.members.list", description="获取群成员列表（官方单页最多 30 条，返回 next_cursor）", version="1", public=True)
    async def api_group_members_list(self, group_openid: str = "", cursor: str = "") -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        return await self._invoke_group_api(
            "获取群成员列表", lambda: self._group_api.list_members(group, cursor=str(cursor or "").strip())
        )

    @API("adapter.group.members.get", description="获取单个群成员的信息（身份、昵称、入群时间）", version="1", public=True)
    async def api_group_member_get(self, group_openid: str = "", member_openid: str = "") -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        member = self._require_member_openids([member_openid])[0]
        return await self._invoke_group_api("获取群成员信息", lambda: self._group_api.get_member(group, member))

    @API("adapter.group.members.remove", description="批量移出群成员（可选同时加入群黑名单）", version="1", public=True)
    async def api_group_members_remove(
        self,
        group_openid: str = "",
        member_openids: Optional[List[str]] = None,
        add_to_blacklist: bool = False,
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        members = self._require_member_openids(member_openids)
        return await self._invoke_group_api(
            "批量移出群成员",
            lambda: self._group_api.remove_members(group, members, add_to_blacklist=bool(add_to_blacklist)),
        )

    @API("adapter.group.blacklist.list", description="查询群黑名单（官方单页最多 100 条）", version="1", public=True)
    async def api_group_blacklist_list(
        self, group_openid: str = "", cursor: str = "", limit: int = 0
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        return await self._invoke_group_api(
            "查询群黑名单",
            lambda: self._group_api.list_blacklist(
                group, cursor=str(cursor or "").strip(), limit=max(0, min(int(limit or 0), BLACKLIST_PAGE_MAX))
            ),
        )

    @API("adapter.group.blacklist.set", description="群黑名单操作：op=add 加入黑名单、op=del 移出黑名单", version="1", public=True)
    async def api_group_blacklist_set(
        self, group_openid: str = "", op: str = "", member_openids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        normalized_op = str(op or "").strip().lower()
        if normalized_op not in BLACKLIST_OPS:
            raise ValueError("op 必须是 add 或 del")
        members = self._require_member_openids(member_openids)
        return await self._invoke_group_api(
            "群黑名单操作", lambda: self._group_api.update_blacklist(group, normalized_op, members)
        )

    @API("adapter.group.join_requests.list", description="拉取入群申请列表（官方单页最多 50 条）", version="1", public=True)
    async def api_group_join_requests_list(
        self, group_openid: str = "", cursor: str = "", limit: int = 0
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        return await self._invoke_group_api(
            "拉取入群申请列表",
            lambda: self._group_api.list_join_requests(
                group, cursor=str(cursor or "").strip(), limit=max(0, min(int(limit or 0), JOIN_REQUEST_PAGE_MAX))
            ),
        )

    @API("adapter.group.join_requests.approve", description="审批入群申请：op=approve 同意、op=decline 拒绝", version="1", public=True)
    async def api_group_join_request_approve(
        self,
        group_openid: str = "",
        member_openid: str = "",
        op: str = "",
        join_request_id: str = "",
        reject_reason: str = "",
        add_to_blacklist: bool = False,
    ) -> Dict[str, Any]:
        group = self._require_group_openid(group_openid)
        normalized_op = str(op or "").strip().lower()
        if normalized_op not in APPROVAL_OPS:
            raise ValueError("op 必须是 approve 或 decline")
        member = self._require_member_openids([member_openid])[0]
        return await self._invoke_group_api(
            "审批入群申请",
            lambda: self._group_api.approve_join_request(
                group,
                member,
                normalized_op,
                join_request_id=str(join_request_id or "").strip(),
                reject_reason=str(reject_reason or "").strip(),
                add_to_blacklist=bool(add_to_blacklist),
            ),
        )

    @API("adapter.group.strategy.create", description="创建入群自动审批策略（group_openids 与 group_ids 二选一）", version="1", public=True)
    async def api_group_strategy_create(
        self,
        group_openids: Optional[List[str]] = None,
        group_ids: Optional[List[int]] = None,
        is_enable: str = "",
        expire_at: str = "",
        remark: str = "",
    ) -> Dict[str, Any]:
        resolved_groups = [
            self._require_group_openid(item) for item in (group_openids or []) if str(item or "").strip()
        ]
        normalized_ids = [int(item) for item in (group_ids or []) if str(item or "").strip()]
        if not resolved_groups and not normalized_ids:
            raise ValueError("group_openids 与 group_ids 必须提供其中一个")
        if len(resolved_groups) > STRATEGY_GROUP_LIMIT or len(normalized_ids) > STRATEGY_GROUP_LIMIT:
            raise ValueError(f"单次最多关联 {STRATEGY_GROUP_LIMIT} 个群")
        return await self._invoke_group_api(
            "创建入群自动审批策略",
            lambda: self._group_api.create_strategy(
                resolved_groups,
                normalized_ids,
                is_enable=str(is_enable or "").strip(),
                expire_at=str(expire_at or "").strip(),
                remark=str(remark or "").strip(),
            ),
        )

    @API("adapter.group.strategy.list", description="查看入群自动审批策略列表（官方单页最多 50 条）", version="1", public=True)
    async def api_group_strategy_list(self, cursor: str = "", limit: int = 0) -> Dict[str, Any]:
        return await self._invoke_group_api(
            "查看入群自动审批策略列表",
            lambda: self._group_api.list_strategies(
                cursor=str(cursor or "").strip(), limit=max(0, min(int(limit or 0), STRATEGY_PAGE_MAX))
            ),
        )

    @API("adapter.group.strategy.update", description="修改入群自动审批策略（开关、到期时间、关联群、备注）", version="1", public=True)
    async def api_group_strategy_update(
        self,
        strategy_id: str = "",
        is_enable: str = "",
        expire_at: str = "",
        group_action: Optional[Dict[str, Any]] = None,
        remark: str = "",
    ) -> Dict[str, Any]:
        normalized_id = str(strategy_id or "").strip()
        if not normalized_id:
            raise ValueError("strategy_id 必须非空")
        normalized_action: Optional[Dict[str, Any]] = None
        if group_action:
            action_op = str(group_action.get("op") or "").strip().lower()
            if action_op not in {"add", "del"}:
                raise ValueError("group_action.op 必须是 add 或 del")
            normalized_action = dict(group_action)
            normalized_action["op"] = action_op
            if normalized_action.get("group_openids"):
                normalized_action["group_openids"] = [
                    self._require_group_openid(item) for item in normalized_action["group_openids"]
                ]
        return await self._invoke_group_api(
            "修改入群自动审批策略",
            lambda: self._group_api.update_strategy(
                normalized_id,
                is_enable=str(is_enable or "").strip(),
                expire_at=str(expire_at or "").strip(),
                group_action=normalized_action,
                remark=str(remark or "").strip(),
            ),
        )

    @API("adapter.group.strategy.delete", description="删除入群自动审批策略", version="1", public=True)
    async def api_group_strategy_delete(self, strategy_id: str = "") -> Dict[str, Any]:
        normalized_id = str(strategy_id or "").strip()
        if not normalized_id:
            raise ValueError("strategy_id 必须非空")
        return await self._invoke_group_api(
            "删除入群自动审批策略", lambda: self._group_api.delete_strategy(normalized_id)
        )

    @API("adapter.group.strategy.whitelist", description="增删入群自动审批策略白名单（op=add/del，单次最多 1 万人）", version="1", public=True)
    async def api_group_strategy_whitelist(
        self, strategy_id: str = "", op: str = "", whitelist_users: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        normalized_id = str(strategy_id or "").strip()
        if not normalized_id:
            raise ValueError("strategy_id 必须非空")
        normalized_op = str(op or "").strip().lower()
        if normalized_op not in BLACKLIST_OPS:
            raise ValueError("op 必须是 add 或 del")
        users = [str(item).strip() for item in (whitelist_users or []) if str(item or "").strip()]
        if not users:
            raise ValueError("whitelist_users 不能为空")
        # 官方要求白名单账号使用字符串，避免 JS 精度问题。
        return await self._invoke_group_api(
            "更新入群自动审批白名单",
            lambda: self._group_api.update_strategy_whitelist(normalized_id, normalized_op, users),
        )

    @API("adapter.group.strategy.execute", description="立即执行入群自动审批策略（官方异步全量扫描，约 10 分钟）", version="1", public=True)
    async def api_group_strategy_execute(self, strategy_id: str = "") -> Dict[str, Any]:
        normalized_id = str(strategy_id or "").strip()
        if not normalized_id:
            raise ValueError("strategy_id 必须非空")
        return await self._invoke_group_api(
            "执行入群自动审批策略", lambda: self._group_api.execute_strategy(normalized_id)
        )

    @API("adapter.group.events", description="拉取最近收到的群事件（成员入群/退群、加群申请等），支持按序号增量获取", version="1", public=True)
    async def api_group_events(self, after_serial: int = 0, limit: int = 50) -> Dict[str, Any]:
        try:
            events = self.snapshot_group_events(after_serial=int(after_serial or 0), limit=int(limit or 50))
        except Exception as exc:
            self.ctx.logger.warning("读取 QQ 群事件失败: %s", exc)
            return {"success": False, "error": str(exc)}
        return {
            "success": True,
            "data": {
                "events": events,
                "latest_serial": self._group_event_serial,
                "events_enabled": self._group_events_active,
            },
        }
