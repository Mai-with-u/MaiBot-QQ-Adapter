from __future__ import annotations

from typing import ClassVar, List, Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "插件"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(default=False, description="启用 QQ 官方机器人连接")
    config_version: str = Field(default="0.4.2", description="配置版本")


class QQOfficialSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "QQ 开放平台"
    __ui_order__: ClassVar[int] = 1

    app_id: str = Field(default="", description="QQ 开放平台机器人的 AppID", json_schema_extra={"label": "AppID"})
    app_secret: str = Field(
        default="",
        description="QQ 开放平台机器人的 AppSecret（ClientSecret）",
        repr=False,
        json_schema_extra={"label": "AppSecret", "x-widget": "password"},
    )
    api_base_url: str = Field(default="https://api.bot.qq.com", description="QQ 开放平台 API 地址")
    unified_account_id: str = Field(
        default="",
        description="统一账号 ID（如 NapCat 机器人 QQ 号）；非空时聊天流按此 ID 归属，与其它 qq 适配器共享上下文",
        json_schema_extra={"label": "关联QQ号"},
    )
    assign_admin_ids: List[str] = Field(
        default_factory=list,
        description="允许执行 /assign_group_id 与 /assign_id 的用户 ID（统一 ID 或 OpenID）；留空不限制",
        json_schema_extra={"label": "QQ Bot管理员账号"},
    )


class MuteSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "禁言工具"
    __ui_order__: ClassVar[int] = 2

    enabled: bool = Field(default=True, description="启用 mute 禁言工具，默认开启，关闭时不向模型提供该工具")
    allowed_groups: List[str] = Field(
        default_factory=list,
        description="允许使用禁言的群（统一群号或 group_openid）；留空不限制",
    )
    admin_users: List[str] = Field(
        default_factory=list,
        description="禁言保护名单（统一 QQ 号或 OpenID），名单内用户不会被禁言",
    )
    min_duration: int = Field(default=60, ge=1, description="最短禁言时长（秒）")
    max_duration: int = Field(default=2592000, ge=1, le=2592000, description="最长禁言时长（秒），官方上限 30 天")


class RecallSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "撤回工具"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(default=True, description="启用 recall_message 撤回工具，默认开启，关闭时不向模型提供该工具")
    allowed_groups: List[str] = Field(
        default_factory=list,
        description="允许使用撤回的群（统一群号或 group_openid）；留空不限制，单聊不受此限制",
    )


class PrivateMenuItem(PluginConfigBase):
    """单聊自定义菜单项，对应官方 /v2/menu 的 Item。"""

    name: str = Field(
        default="",
        description="按钮名称，最多 10 个字符，一个中文汉字按 2 个字符计算",
        json_schema_extra={"label": "按钮名称"},
    )
    type: Literal["send_message", "link", "menu"] = Field(
        default="send_message",
        description="按钮类型：填入指令 / 跳转链接 / 二级菜单",
        json_schema_extra={"label": "按钮类型"},
    )
    value: str = Field(
        default="",
        description="按钮类型为「填入指令」时填点击后填入输入框的内容（如 /help）；为「跳转链接」时填以 https:// 开头的地址",
        json_schema_extra={"label": "按钮内容（指令或 https 链接）"},
    )
    sub_items: List[str] = Field(
        default_factory=list,
        description="仅「二级菜单」类型生效，最多 5 项，每项为点击后填入输入框的指令；二级菜单不支持再嵌套",
        json_schema_extra={"label": "二级菜单项（仅「二级菜单」类型）", "max_items": 5},
    )


class GroupMenuItem(PluginConfigBase):
    """群聊指令面板项，对应官方 /v2/panels 的 PanelItem。"""

    name: str = Field(
        default="",
        description="指令文本，最多 14 个字符，约 7 个中文汉字；点击后填入群聊输入框",
        json_schema_extra={"label": "指令文本"},
    )
    description: str = Field(
        default="",
        description="指令描述，最多 30 个字符，约 15 个中文汉字",
        json_schema_extra={"label": "指令描述"},
    )
    type: Literal["command", "link"] = Field(
        default="command",
        description="元素类型：指令 / 跳转链接",
        json_schema_extra={"label": "元素类型"},
    )
    link: str = Field(
        default="",
        description="仅「跳转链接」类型生效，必须以 https:// 开头",
        json_schema_extra={"label": "跳转链接（仅「跳转链接」类型）"},
    )
    only_admin: bool = Field(
        default=False,
        description="仅群管理员可点击该项",
        json_schema_extra={"label": "仅群管理员可见"},
    )


class MenuSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "指令菜单"
    __ui_order__: ClassVar[int] = 4

    private_enabled: bool = Field(
        default=False,
        description="启用后按下方列表下发所有单聊的自定义菜单；关闭时不改动平台现有菜单",
        json_schema_extra={"label": "启用单聊菜单"},
    )
    private_items: List[PrivateMenuItem] = Field(
        default_factory=list,
        description="单聊自定义菜单项，最多 10 项；留空表示清空平台现有菜单",
        json_schema_extra={"label": "单聊菜单项", "max_items": 10},
    )
    group_enabled: bool = Field(
        default=False,
        description="启用后按下方列表下发所有群聊的指令面板；关闭时不改动平台现有面板",
        json_schema_extra={"label": "启用群聊面板"},
    )
    group_items: List[GroupMenuItem] = Field(
        default_factory=list,
        description="群聊指令面板项，最多 20 项；留空表示删除适配器创建的全局面板",
        json_schema_extra={"label": "群聊面板项", "max_items": 20},
    )


class AdvancedSettings(PluginConfigBase):
    __ui_label__: ClassVar[str] = "高级设置"
    __ui_order__: ClassVar[int] = 5

    reconnect_delay_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=300.0,
        description="断线后等待多久重新连接，单位为秒",
        json_schema_extra={"label": "断线重连间隔（秒）"},
    )


class AdapterSettings(PluginConfigBase):
    plugin: PluginSettings = Field(default_factory=PluginSettings)
    qq_official: QQOfficialSettings = Field(default_factory=QQOfficialSettings)
    mute: MuteSettings = Field(default_factory=MuteSettings)
    recall: RecallSettings = Field(default_factory=RecallSettings)
    menu: MenuSettings = Field(default_factory=MenuSettings)
    advanced: AdvancedSettings = Field(default_factory=AdvancedSettings)
