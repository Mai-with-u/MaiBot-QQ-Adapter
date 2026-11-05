import asyncio
import base64
import time

import pytest

from qq_official_adapter.plugin import QQOfficialAdapterPlugin

GROUP_OPENID = "group-openid"
REPLY_ID = "ROBOT1.0_reply"
FILE_INFO = "FILEINFO_uploaded"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8
SILK = b"\x02#!SILK_V3" + b"\x00" * 8
MP3 = b"ID3\x03\x00\x00\x00" + b"\x00" * 8


class _Logger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class _Ctx:
    logger = _Logger()


class _FakePut:
    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    """只实现分片上传用到的 put；预签名地址不得携带鉴权头。"""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.closed = False
        self.puts = []

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        return _FakePut(self.status)


async def _make_plugin(parts=None, status: int = 200):
    """构造绕过 SDK 初始化的插件，捕获 _api_post 与分片 PUT。"""
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ready = True
    plugin._id_map = {"group": {}, "user": {}}
    plugin._ref_idx = {}
    plugin._replies = {}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = _Ctx()
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    session = _FakeSession(status=status)
    plugin._session = session
    captured = []
    upload_parts = parts if parts is not None else [{"index": 1, "presigned_url": "https://upload.example/1"}]

    async def _fake_api_post(path, payload):
        captured.append((path, dict(payload)))
        if path.endswith("/upload_prepare"):
            return {
                "upload_id": "UPLOAD1",
                "block_size": str(len(PNG)),
                "parts": upload_parts,
            }
        if path.endswith("/upload_part_finish"):
            return {}
        if path.endswith("/files"):
            return {"file_uuid": "uuid1", "file_info": FILE_INFO, "ttl": 0}
        return {"id": "ROBOT1.0_out", "ext_info": {"ref_idx": "REFIDX_out=="}}

    plugin._api_post = _fake_api_post
    return plugin, captured, session


def _media_message(segment):
    return {
        "message_info": {"group_info": {"group_id": GROUP_OPENID}, "additional_config": {}},
        "raw_message": [segment],
    }


def _post_payload(captured, suffix):
    for path, payload in captured:
        if path.endswith(suffix):
            return payload
    raise AssertionError(f"未找到 {suffix} 请求: {[path for path, _ in captured]}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("segment_type", "binary", "expected_file_type"),
    [
        ("image", PNG, 1),
        ("emoji", PNG, 1),
        ("video", MP4, 2),
        ("voice", SILK, 3),
    ],
)
async def test_media_upload_uses_official_file_type(segment_type, binary, expected_file_type) -> None:
    # 富媒体统一按官方分片上传取得 file_info，再以 msg_type=7 发送；
    # file_type 必须与内容匹配（1 图片、2 视频、3 语音），否则平台报 850019。
    plugin, captured, session = await _make_plugin()
    message = _media_message({"type": segment_type, "data": "", "binary_data_base64": base64.b64encode(binary).decode()})
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert _post_payload(captured, "/upload_prepare")["file_type"] == expected_file_type
    assert _post_payload(captured, "/files")["file_type"] == expected_file_type
    assert _post_payload(captured, "/files")["srv_send_msg"] is False
    assert len(session.puts) == 1
    # 预签名地址自带鉴权，PUT 不能携带 QQBot Authorization 头。
    assert "headers" not in session.puts[0][1]
    payload = _post_payload(captured, "/messages")
    assert payload["msg_type"] == 7
    assert payload["media"]["file_info"] == FILE_INFO
    assert payload.get("msg_id") == REPLY_ID


@pytest.mark.asyncio
async def test_voice_mp3_is_accepted_as_voice() -> None:
    # 官方语音支持 silk/mp3/wav/ogg，四种格式都按 file_type=3 直传，无需本地转码。
    plugin, captured, _ = await _make_plugin()
    message = _media_message({"type": "voice", "data": "", "binary_data_base64": base64.b64encode(MP3).decode()})
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert _post_payload(captured, "/upload_prepare")["file_type"] == 3
    assert _post_payload(captured, "/files")["file_name"].endswith(".mp3")


@pytest.mark.asyncio
async def test_multi_part_upload_handles_one_based_index() -> None:
    # 官方文档写分片序号从 0 开始，实测（2026-09-30）返回从 1 开始，
    # 偏移必须按实际最小序号计算，否则会漏传或错位。
    parts = [
        {"index": 1, "presigned_url": "https://upload.example/1"},
        {"index": 2, "presigned_url": "https://upload.example/2"},
    ]
    plugin, captured, session = await _make_plugin(parts=parts)
    message = _media_message(
        {"type": "image", "data": "", "binary_data_base64": base64.b64encode(PNG + PNG).decode()}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert [url for url, _ in session.puts] == ["https://upload.example/1", "https://upload.example/2"]
    assert session.puts[0][1]["data"] == PNG
    assert session.puts[1][1]["data"] == PNG
    finishes = [payload for path, payload in captured if path.endswith("/upload_part_finish")]
    assert [payload["part_index"] for payload in finishes] == [1, 2]


@pytest.mark.asyncio
async def test_media_upload_reports_part_put_failure() -> None:
    # 分片 PUT 失败必须给出可读错误，且不上报成功（避免宿主以为消息已送达）。
    plugin, captured, _ = await _make_plugin(status=403)
    message = _media_message({"type": "video", "data": "", "binary_data_base64": base64.b64encode(MP4).decode()})
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is False
    assert "分片 1 上传失败: HTTP 403" in result["error"]
    assert not [path for path, _ in captured if path.endswith("/messages")]


@pytest.mark.asyncio
async def test_file_segment_with_url_is_transferred_by_platform() -> None:
    # file 段只带 URL 时不做本地下载，交给官方转存（官方 URL 上传路径）。
    plugin, captured, session = await _make_plugin()
    message = _media_message(
        {"type": "file", "data": {"name": "clip.mp4", "url": "https://example.com/clip.mp4", "mime_type": "video/mp4"}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert not [path for path, _ in captured if path.endswith("/upload_prepare")]
    assert session.puts == []
    files_payload = _post_payload(captured, "/files")
    assert files_payload["url"] == "https://example.com/clip.mp4"
    assert files_payload["file_type"] == 2
    assert _post_payload(captured, "/messages")["media"]["file_info"] == FILE_INFO


@pytest.mark.asyncio
async def test_file_segment_without_content_reports_error() -> None:
    # 宿主只给了文件名、既无 base64 也无 URL 时，必须明确报错而不是发出空消息。
    plugin, captured, _ = await _make_plugin()
    message = _media_message({"type": "file", "data": {"name": "doc.pdf", "size": "1024"}})
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is False
    assert "缺少文件内容" in result["error"]
    assert not [path for path, _ in captured if path.endswith("/messages")]


@pytest.mark.asyncio
async def test_file_segment_with_base64_is_sent_as_plain_file() -> None:
    # 普通文件（pdf/doc/zip…）走 file_type=4 分片上传，官方 SDK 的 sendFile 也是这条路。
    plugin, captured, session = await _make_plugin()
    pdf = b"%PDF-1.7 report"
    message = _media_message(
        {"type": "file", "data": {"name": "report.pdf", "base64": base64.b64encode(pdf).decode()}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert _post_payload(captured, "/upload_prepare")["file_type"] == 4
    assert _post_payload(captured, "/files")["file_type"] == 4
    # 文件名必须原样带出去，否则文件卡片显示不出真实类型。
    assert _post_payload(captured, "/upload_prepare")["file_name"] == "report.pdf"
    assert len(session.puts) == 1
    payload = _post_payload(captured, "/messages")
    assert payload["msg_type"] == 7
    assert payload["media"]["file_info"] == FILE_INFO


@pytest.mark.asyncio
async def test_file_segment_with_url_keeps_file_name_for_plain_file() -> None:
    # 普通文件走 URL 转存时必须带 file_name，平台靠它生成文件卡片标题。
    plugin, captured, session = await _make_plugin()
    message = _media_message(
        {"type": "file", "data": {"name": "report.pdf", "url": "https://example.com/report.pdf"}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert not [path for path, _ in captured if path.endswith("/upload_prepare")]
    assert session.puts == []
    files_payload = _post_payload(captured, "/files")
    assert files_payload["file_type"] == 4
    assert files_payload["file_name"] == "report.pdf"
    assert files_payload["url"] == "https://example.com/report.pdf"


@pytest.mark.asyncio
async def test_file_segment_without_extension_defaults_to_plain_file() -> None:
    # 既无后缀也识别不出内容时仍按普通文件发送，扩展名退化为 bin 而不是直接失败。
    plugin, captured, _ = await _make_plugin()
    message = _media_message(
        {"type": "file", "data": {"base64": base64.b64encode(b"unknown-bytes").decode()}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    prepare = _post_payload(captured, "/upload_prepare")
    assert prepare["file_type"] == 4
    assert prepare["file_name"].endswith(".bin")


@pytest.mark.asyncio
async def test_file_segment_trusts_content_over_extension() -> None:
    # 文件名写着 mp4 但内容不是 mp4：按内容判定为普通文件，避免平台以 850019 拒绝上传。
    plugin, captured, _ = await _make_plugin()
    message = _media_message(
        {"type": "file", "data": {"name": "fake.mp4", "base64": base64.b64encode(b"%PDF-1.7 x").decode()}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    prepare = _post_payload(captured, "/upload_prepare")
    assert prepare["file_type"] == 4
    assert prepare["file_name"] == "fake.mp4"


@pytest.mark.asyncio
async def test_unrecognized_media_payload_reports_error() -> None:
    # 段声明是 video 但内容不是 mp4：按文件头识别失败，报官方支持格式。
    plugin, captured, _ = await _make_plugin()
    message = _media_message(
        {"type": "video", "data": "", "binary_data_base64": base64.b64encode(b"%PDF-1.7 file").decode()}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is False
    assert "不是官方支持的媒体格式" in result["error"]
    assert not [path for path, _ in captured if path.endswith("/messages")]
