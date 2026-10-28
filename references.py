"""短期、精确匹配的 QQ 引用索引；不持久化，也不推断索引前缀等价性。"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, OrderedDict, Tuple

import collections
import re
import time

# 本地内存策略，不代表 QQ 官方引用索引有效期。
REFERENCE_TTL = 24 * 3600
REFERENCE_CAPACITY = 2000
MAX_VALUE_LENGTH = 2048
MAX_EXT_ITEMS = 32
KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
Scope = Tuple[str, str, str]  # 实际 READY 账号、group/private、原始目标 OpenID
Key = Tuple[Scope, str]


def valid_value(value: Any) -> bool:
    """不猜测不透明 ID 的 Base64/REFIDX/TMP 语法，只允许有界、无空白 ASCII。"""
    return (isinstance(value, str) and 0 < len(value) <= MAX_VALUE_LENGTH
            and all(33 <= ord(char) <= 126 for char in value))


def parse_scene_indexes(payload: Mapping[str, Any]) -> Tuple[str, str]:
    """ext 按首个 = 分割；重复键（即使值相同）或畸形字段使整个 ext 不可解析。"""
    scene = payload.get("message_scene")
    if not isinstance(scene, Mapping):
        return "", ""
    ext = scene.get("ext")
    if not isinstance(ext, list) or len(ext) > MAX_EXT_ITEMS:
        return "", ""
    seen: Dict[str, str] = {}
    for item in ext:
        if not isinstance(item, str) or len(item) > MAX_VALUE_LENGTH + 65:
            return "", ""
        key, separator, value = item.partition("=")
        if not separator or not KEY_PATTERN.fullmatch(key) or not valid_value(value) or key in seen:
            return "", ""
        seen[key] = value
    # 不保留 auth_token 或未知字段。
    return seen.get("msg_idx", ""), seen.get("ref_msg_idx", "")


class ReferenceCache:
    """单事件循环同步访问；双向映射共用 TTL/容量，重放不续期。

    任一双向冲突会清空并隔离整个 scope，避免随后重放重新建立歧义映射。
    隔离标记也占容量，按同一 TTL/FIFO 淘汰；遗忘后不再保证历史冲突检测。
    """

    def __init__(self, capacity: int = REFERENCE_CAPACITY, ttl: float = REFERENCE_TTL,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity < 1 or ttl <= 0:
            raise ValueError("引用缓存容量和 TTL 必须为正")
        self._capacity = capacity
        self._ttl = ttl
        self._clock = clock
        self._forward: OrderedDict[Key, Tuple[float, str]] = collections.OrderedDict()
        self._reverse: Dict[Key, str] = {}

    def clear(self) -> None:
        self._forward.clear()
        self._reverse.clear()

    def _remove(self, key: Key) -> None:
        _, index = self._forward.pop(key)
        if index:
            del self._reverse[(key[0], index)]

    def prune(self) -> None:
        now = self._clock()
        while self._forward:
            key, (stored, _) = next(iter(self._forward.items()))
            if now - stored < self._ttl and len(self._forward) <= self._capacity:
                break
            self._remove(key)

    @staticmethod
    def _valid_scope(scope: Scope) -> bool:
        return scope[1] in {"group", "private"} and valid_value(scope[0]) and valid_value(scope[2])

    def remember(self, scope: Scope, message_id: Any, index: Any) -> bool:
        self.prune()
        if not self._valid_scope(scope) or not valid_value(message_id) or not valid_value(index):
            return False
        if (scope, "") in self._forward:
            return False
        key = (scope, message_id)
        previous = self._forward.get(key)
        reverse = self._reverse.get((scope, index))
        if (previous is not None and previous[1] != index) or (reverse is not None and reverse != message_id):
            for scoped_key in tuple(self._forward):
                if scoped_key[0] == scope:
                    self._remove(scoped_key)
            self._forward[(scope, "")] = (self._clock(), "")
            self.prune()
            return False
        if previous is None:
            self._forward[key] = (self._clock(), index)
            self._reverse[(scope, index)] = message_id
            self.prune()
        return True

    def index_for(self, scope: Scope, message_id: Any) -> str:
        self.prune()
        if not valid_value(message_id):
            return ""
        entry = self._forward.get((scope, message_id))
        return entry[1] if entry else ""

    def id_for(self, scope: Scope, index: Any) -> str:
        self.prune()
        return self._reverse.get((scope, index), "") if valid_value(index) else ""


def reply_target(message: Mapping[str, Any]) -> str:
    """只接受标准 reply.data.target_message_id；多个/畸形 reply 不产生引用。"""
    segments = message.get("raw_message")
    if not isinstance(segments, list):
        return ""
    replies = [part for part in segments if isinstance(part, Mapping) and part.get("type") == "reply"]
    if len(replies) != 1:
        return ""
    data = replies[0].get("data")
    target = data.get("target_message_id") if isinstance(data, Mapping) else None
    return target if valid_value(target) else ""
