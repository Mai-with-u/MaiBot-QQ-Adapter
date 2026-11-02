"""Temporary opt-in structural probes. No message content or raw identifiers."""

from typing import Any, Dict, List, Mapping, Set

import hashlib
import hmac
import json
import math
import os
import time

from .references import KEY_PATTERN, MAX_EXT_ITEMS, MAX_VALUE_LENGTH, valid_value


def scene_fields(payload: Mapping[str, Any]) -> Dict[str, bool]:
    """Mirror ext validation for observation only; never infer a reference."""
    scene = payload.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, Mapping) else None
    valid = isinstance(ext, list) and len(ext) <= MAX_EXT_ITEMS
    seen: Set[str] = set()
    if valid:
        for item in ext:
            if not isinstance(item, str) or len(item) > MAX_VALUE_LENGTH + 65:
                valid = False
                break
            key, separator, value = item.partition("=")
            if not separator or not KEY_PATTERN.fullmatch(key) or not valid_value(value) or key in seen:
                valid = False
                break
            seen.add(key)
    return {"ext_present": isinstance(scene, Mapping) and "ext" in scene, "ext_valid": valid}


class ReferenceDebug:
    def __init__(self) -> None:
        self._remaining = 200
        self._key = os.environ.get("QQ_REFERENCE_DEBUG_SALT", "").encode()
        try:
            until = float(os.environ.get("QQ_REFERENCE_DEBUG_UNTIL", "0"))
        except ValueError:
            until = 0.0
        now = time.time()
        duration = min(900.0, until - now) if math.isfinite(until) else 0.0
        self._deadline = time.monotonic() + max(0.0, duration)
        self._until = until
        self._enabled = len(self._key) >= 32 and duration > 0

    def active(self) -> bool:
        return (self._enabled and self._remaining > 0
                and time.monotonic() < self._deadline and time.time() < self._until)

    def token(self, value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 2048:
            return ""
        return hmac.new(self._key, b"qq-reference-message-v1\0" + value.encode(), hashlib.sha256).hexdigest()[:24]

    def summary(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        raw = message.get("raw_message")
        counts = dict.fromkeys(("text", "reply", "image", "emoji", "at", "other", "binary"), 0)
        targets: List[str] = []
        if isinstance(raw, list):
            for part in raw:
                if not isinstance(part, dict):
                    counts["other"] += 1
                    continue
                kind = part.get("type")
                kind = kind if isinstance(kind, str) and kind in counts and kind != "binary" else "other"
                counts[kind] += 1
                if kind in {"image", "emoji"}:
                    binary = part.get("binary_data_base64")
                    counts["binary"] += int(isinstance(binary, str) and bool(binary))
                if kind == "reply":
                    data = part.get("data")
                    target = data.get("target_message_id") if isinstance(data, dict) else data
                    if len(targets) < 4:
                        targets.append(self.token(target))
        return {"mid": self.token(message.get("message_id")), "raw_list": isinstance(raw, list),
                "counts": counts, "targets": targets, "reply_to": self.token(message.get("reply_to"))}

    def emit(self, ctx: Any, stage: str, message: Mapping[str, Any], *,
             target: Any = None, **fields: Any) -> None:
        if not self.active():
            return
        # Probe failures must never replace the application's return/exception.
        try:
            self._remaining -= 1
            record = {"stage": stage, "seq": 200 - self._remaining,
                      "run": self.token("diagnostic-run"), **self.summary(message)}
            if target is not None:
                record["target"] = self.token(target)
            # Call sites supply fixed field names. Only booleans/integers survive;
            # arbitrary SDK values, exception strings and segment types never do.
            record.update({key: value for key, value in fields.items() if type(value) in (bool, int)})
            ctx.logger.info("[QQREF-DEBUG-v1] %s", json.dumps(record, sort_keys=True))
        except Exception:
            pass


probe = ReferenceDebug()
