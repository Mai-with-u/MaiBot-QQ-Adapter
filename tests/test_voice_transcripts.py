"""Offline regression tests for QQ platform transcripts; no gateway or HTTP I/O."""
from __future__ import annotations

import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from qq_official_adapter.plugin import QQOfficialAdapterPlugin


def make_plugin():
    plugin = QQOfficialAdapterPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    gateway = SimpleNamespace(update_state=AsyncMock(return_value=True), route_message=AsyncMock(return_value=True))
    plugin._set_context(SimpleNamespace(logger=Mock(), gateway=gateway))
    plugin._download_attachment = AsyncMock(side_effect=AssertionError("unexpected audio download"))
    return plugin


def voice(**overrides):
    return {"content_type": "voice", "filename": "test.amr", "size": 12,
            "url": "https://qqbot.ugcimg.cn/test.amr", **overrides}


class VoiceTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_transcript_survives_unavailable_audio(self):
        plugin = make_plugin()
        for url in ["https://qqbot.ugcimg.cn/test.amr", "", "https://untrusted.example/test.amr"]:
            with self.subTest(url=url):
                segments, labels, picture, emoji = await plugin._build_attachments({
                    "attachments": [voice(url=url, asr_refer_text="  你好，现在听得到吗？  ")]
                })
                self.assertEqual(segments, [{"type": "text", "data": "[语音: 你好，现在听得到吗？]"}])
                self.assertEqual(labels, [segments[0]["data"]])
                self.assertFalse(picture or emoji)
        plugin._download_attachment.assert_not_awaited()

    async def test_voice_type_variants_use_transcripts(self):
        plugin = make_plugin()
        for kind in ["voice", "audio/amr", "audio/silk", "audio/wav"]:
            with self.subTest(kind=kind):
                segments, *_ = await plugin._build_attachments({"attachments": [voice(content_type=kind, asr_refer_text="测试")]})
                self.assertEqual(segments, [{"type": "text", "data": "[语音: 测试]"}])
        segments, *_ = await plugin._build_attachments({"attachments": [voice(content_type="application/octet-stream", type="voice", asr_refer_text="测试")]})
        self.assertEqual(segments[0]["data"], "[语音: 测试]")
        plugin._download_attachment.assert_not_awaited()

    async def test_missing_or_invalid_transcript_leaves_asr_content_empty(self):
        plugin = make_plugin()
        plugin._download_attachment = AsyncMock(return_value=b"fake-audio")
        for value in [None, "", " \n\t ", 123, {"text": "not a string"}, ["invalid"]]:
            with self.subTest(value=value):
                segments, labels, *_ = await plugin._build_attachments({"attachments": [voice(asr_refer_text=value)]})
                self.assertEqual(segments[0]["type"], "voice")
                self.assertEqual(segments[0]["data"], "")
                self.assertEqual(base64.b64decode(segments[0]["binary_data_base64"]), b"fake-audio")
                self.assertEqual(labels, ["[语音]"])
        self.assertEqual(plugin._download_attachment.await_count, 6)

    async def test_no_transcript_and_failed_download_keeps_file_fallback(self):
        plugin = make_plugin()
        plugin._download_attachment = AsyncMock(return_value=b"")
        segments, labels, *_ = await plugin._build_attachments({"attachments": [voice()]})
        self.assertEqual(segments[0]["type"], "file")
        self.assertEqual(segments[0]["data"]["mime_type"], "voice")
        self.assertEqual(labels, ["[语音]"])

    async def test_image_and_file_processing_is_preserved(self):
        plugin = make_plugin()
        plugin._download_attachment = AsyncMock(return_value=b"fake-image")
        segments, labels, picture, emoji = await plugin._build_attachments({"attachments": [
            {"content_type": "image/png", "url": "https://multimedia.nt.qq.com.cn/test.png", "filename": "test.png"},
            {"content_type": "application/pdf", "url": "https://multimedia.nt.qq.com.cn/test.pdf", "filename": "test.pdf"},
        ]})
        self.assertEqual([segment["type"] for segment in segments], ["image", "file"])
        self.assertEqual(base64.b64decode(segments[0]["binary_data_base64"]), b"fake-image")
        self.assertEqual(segments[1]["data"]["name"], "test.pdf")
        self.assertTrue(picture)
        self.assertFalse(emoji)
        plugin._download_attachment.assert_awaited_once()

    async def test_receive_routes_body_and_multiple_transcripts_once(self):
        plugin = make_plugin()
        plugin._ready = True
        plugin._inbound_ready.set()
        payload = {"id": "offline-voice-regression", "author": {"user_openid": "synthetic-user"},
                   "timestamp": "2026-10-31T18:38:33+08:00", "content": "原有正文",
                   "attachments": [voice(asr_refer_text="第一段"), voice(asr_refer_text="第二段")]}
        await plugin._receive_message("C2C_MESSAGE_CREATE", payload, account_id="synthetic-bot", bot_name="Bot")
        plugin.ctx.gateway.route_message.assert_awaited_once()
        sent = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
        self.assertEqual(sent["raw_message"], [
            {"type": "text", "data": "原有正文"},
            {"type": "text", "data": "[语音: 第一段]"},
            {"type": "text", "data": "[语音: 第二段]"},
        ])
        self.assertEqual(sent["processed_plain_text"], "原有正文 [语音: 第一段] [语音: 第二段]")
        self.assertFalse(sent["is_command"])
        plugin._download_attachment.assert_not_awaited()

    async def test_transcribed_command_is_not_a_gateway_management_command(self):
        plugin = make_plugin()
        plugin._ready = True
        plugin._inbound_ready.set()
        payload = {"id": "offline-transcribed-command", "author": {"user_openid": "synthetic-user"},
                   "timestamp": "2026-10-31T18:38:33+08:00", "content": "",
                   "attachments": [voice(asr_refer_text="/assign_id 123")]}
        await plugin._receive_message("C2C_MESSAGE_CREATE", payload, account_id="synthetic-bot", bot_name="Bot")
        sent = plugin.ctx.gateway.route_message.call_args.kwargs["message"]
        self.assertFalse(sent["is_command"])
        self.assertEqual(sent["raw_message"], [{"type": "text", "data": "[语音: /assign_id 123]"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
