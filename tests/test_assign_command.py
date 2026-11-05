import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import Mock

import pytest

from qq_official_adapter.codec import parse_command_text
from qq_official_adapter.plugin import ASSIGN_GROUP_COMMAND, ASSIGN_USER_COMMAND, QQOfficialAdapterPlugin

BOT_OPENID = "BOT_OPENID_999"
GROUP_OPENID = "GROUP_OPENID_AAA"
USER_OPENID = "USER_OPENID_XYZ"
SENDER_OPENID = "SENDER_OPENID_000"
QQ_NUMBER = "675130315"


class _Logger:
    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


async def _make_plugin() -> Tuple[QQOfficialAdapterPlugin, List[str], List[Tuple[str, str, str]]]:
    """构造已注入配置的插件，拦截 _save_id_map 与 _reply_to_command。"""
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    plugin._set_context(SimpleNamespace(logger=_Logger(), gateway=SimpleNamespace(update_state=Mock())))
    plugin._id_map = {"group": {}, "user": {}}
    plugin._account_id = BOT_OPENID
    plugin._bot_name = "Tama"
    plugin._ready = True
    replies: List[str] = []
    saved: List[Tuple[str, str, str]] = []

    async def _fake_reply(event: str, data: Dict[str, Any], text: str) -> None:
        replies.append(text)

    def _fake_save() -> None:
        for kind in ("group", "user"):
            for openid, value in plugin._id_map[kind].items():
                saved.append((kind, openid, value))

    plugin._reply_to_command = _fake_reply
    plugin._save_id_map = _fake_save
    return plugin, replies, saved


def _group_payload(content: str) -> Dict[str, Any]:
    """构造全量群消息：正文里的 <@!openid> 是官方下发的 mention 标记。"""
    return {
        "id": "MSG_ID_1",
        "group_openid": GROUP_OPENID,
        "author": {"member_openid": SENDER_OPENID},
        "content": content,
        "timestamp": "2026-11-05T00:00:00Z",
    }


def _private_payload(content: str) -> Dict[str, Any]:
    return {
        "id": "MSG_ID_1",
        "author": {"user_openid": SENDER_OPENID},
        "content": content,
        "timestamp": "2026-11-05T00:00:00Z",
    }


@pytest.mark.parametrize(
    "raw, expected_name, expected_args",
    [
        (f"  {ASSIGN_GROUP_COMMAND}  {QQ_NUMBER}  ", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        (f"{ASSIGN_GROUP_COMMAND}\n{QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        # 全角空格（U+3000）是中文输入法下的高频事故，必须与半角等价。
        (f"{ASSIGN_GROUP_COMMAND}\u3000{QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        (f"{ASSIGN_GROUP_COMMAND}\u00a0{QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        # 句首 mention：协议标记与客户端渲染出的 @昵称 都要抹掉。
        (f"<@!{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        (f"<@{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        (f"@Tama {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        (f"@Tama\u3000{ASSIGN_GROUP_COMMAND} {QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        # `/cmd@Bot` 后缀不属于命令名。
        (f"{ASSIGN_GROUP_COMMAND}@Tama {QQ_NUMBER}", ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        # 连字符与下划线等价。
        ("/assign-group-id " + QQ_NUMBER, ASSIGN_GROUP_COMMAND, [QQ_NUMBER]),
        # @ 对象不是参数，不应被当成 ID 值。
        (f"{ASSIGN_USER_COMMAND} <@!{USER_OPENID}> {QQ_NUMBER}", ASSIGN_USER_COMMAND, [QQ_NUMBER]),
        ("", "", []),
    ],
)
def test_parse_command_text_normalizes(raw: str, expected_name: str, expected_args: List[str]) -> None:
    name, args = parse_command_text(raw)
    assert name == expected_name
    assert args == expected_args


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        f"{ASSIGN_GROUP_COMMAND} {QQ_NUMBER}",
        f"<@!{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}",
        f"<@{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}",
        f"@Tama {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}",
        f"<@!{BOT_OPENID}>\u3000{ASSIGN_GROUP_COMMAND}\u3000{QQ_NUMBER}",
        f"{ASSIGN_GROUP_COMMAND}@{BOT_OPENID} {QQ_NUMBER}",
        f"/assign-group-id {QQ_NUMBER}",
    ],
)
async def test_group_assign_hits_regardless_of_mention_and_whitespace(content: str) -> None:
    # 群里触发官方机器人必须 @ 它，命令因此永远带 mention 前缀；这七种写法都必须绑定成功。
    plugin, replies, saved = await _make_plugin()
    await plugin._receive_message("GROUP_MESSAGE_CREATE", _group_payload(content), account_id=BOT_OPENID, bot_name="Tama")
    assert plugin._id_map["group"] == {GROUP_OPENID: QQ_NUMBER}
    assert plugin._id_map["user"] == {}
    assert saved == [("group", GROUP_OPENID, QQ_NUMBER)]
    assert replies and QQ_NUMBER in replies[0]


@pytest.mark.asyncio
async def test_bot_mention_prefix_no_longer_misroutes_to_user_binding() -> None:
    # 回归 issue #4 的次生 bug：带 mention 前缀时曾把群绑静默判成用户绑。
    plugin, _, _ = await _make_plugin()
    await plugin._receive_message(
        "GROUP_MESSAGE_CREATE",
        _group_payload(f"<@!{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}"),
        account_id=BOT_OPENID,
        bot_name="Tama",
    )
    assert plugin._id_map["user"] == {}


@pytest.mark.asyncio
async def test_private_assign_user_binds_sender() -> None:
    plugin, replies, _ = await _make_plugin()
    await plugin._receive_message(
        "C2C_MESSAGE_CREATE", _private_payload(f"{ASSIGN_USER_COMMAND} {QQ_NUMBER}"), account_id=BOT_OPENID, bot_name="Tama"
    )
    assert plugin._id_map["user"] == {SENDER_OPENID: QQ_NUMBER}
    assert replies and "绑定" in replies[0]


@pytest.mark.asyncio
async def test_assign_user_prefers_mentioned_user() -> None:
    # 「@ 对方为其绑定」的用法：句首 mention 被归一化抹掉，但命令后的 @ 对象仍要生效。
    plugin, replies, _ = await _make_plugin()
    await plugin._receive_message(
        "GROUP_MESSAGE_CREATE",
        _group_payload(f"<@!{BOT_OPENID}> {ASSIGN_USER_COMMAND} {QQ_NUMBER} <@!{USER_OPENID}>"),
        account_id=BOT_OPENID,
        bot_name="Tama",
    )
    assert plugin._id_map["user"] == {USER_OPENID: QQ_NUMBER}
    assert replies and "被 @ 的用户" in replies[0]


@pytest.mark.asyncio
async def test_group_command_in_private_chat_reports_scope_error() -> None:
    plugin, replies, _ = await _make_plugin()
    await plugin._receive_message(
        "C2C_MESSAGE_CREATE", _private_payload(f"{ASSIGN_GROUP_COMMAND} {QQ_NUMBER}"), account_id=BOT_OPENID, bot_name="Tama"
    )
    assert plugin._id_map["group"] == {}
    assert replies and "仅支持群聊使用" in replies[0]


@pytest.mark.asyncio
async def test_assign_prefix_typo_replies_usage_instead_of_leaking_into_chat() -> None:
    # 拼错命令时不再静默进聊天流，而是回一条用法提示。
    plugin, replies, _ = await _make_plugin()
    await plugin._receive_message("GROUP_MESSAGE_CREATE", _group_payload("/assign_group 123"), account_id=BOT_OPENID, bot_name="Tama")
    assert plugin._id_map["group"] == {}
    assert replies and ASSIGN_GROUP_COMMAND in replies[0] and ASSIGN_USER_COMMAND in replies[0]


@pytest.mark.asyncio
async def test_plain_text_still_enters_chat_flow() -> None:
    # 非管理命令必须照旧进入聊天流：用 ValueError 短路到「忽略消息」分支，确认未被拦截。
    plugin, replies, _ = await _make_plugin()

    async def _raise(data: Any) -> Any:
        raise ValueError("消息不包含文本或可处理的附件")

    plugin._build_attachments = _raise
    await plugin._receive_message("GROUP_MESSAGE_CREATE", _group_payload("你好"), account_id=BOT_OPENID, bot_name="Tama")
    assert replies == []


@pytest.mark.asyncio
async def test_admin_list_blocks_unlisted_sender() -> None:
    plugin, replies, _ = await _make_plugin()
    plugin._assign_admin_ids = ["123456"]
    await plugin._receive_message(
        "GROUP_MESSAGE_CREATE",
        _group_payload(f"<@!{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}"),
        account_id=BOT_OPENID,
        bot_name="Tama",
    )
    assert plugin._id_map["group"] == {}
    assert replies and "管理员" in replies[0]


@pytest.mark.asyncio
async def test_invalid_value_replies_usage() -> None:
    plugin, replies, _ = await _make_plugin()
    await plugin._receive_message(
        "GROUP_MESSAGE_CREATE",
        _group_payload(f"<@!{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}（主群）"),
        account_id=BOT_OPENID,
        bot_name="Tama",
    )
    assert plugin._id_map["group"] == {}
    assert replies and "用法" in replies[0]


@pytest.mark.asyncio
async def test_receive_message_does_not_block_event_loop() -> None:
    # 回归守卫：命令处理全程 await，不引入同步阻塞调用。
    plugin, _, _ = await _make_plugin()
    task = asyncio.create_task(
        plugin._receive_message(
            "GROUP_MESSAGE_CREATE",
            _group_payload(f"<@!{BOT_OPENID}> {ASSIGN_GROUP_COMMAND} {QQ_NUMBER}"),
            account_id=BOT_OPENID,
            bot_name="Tama",
        )
    )
    await asyncio.wait_for(task, timeout=5)
    assert plugin._id_map["group"] == {GROUP_OPENID: QQ_NUMBER}
