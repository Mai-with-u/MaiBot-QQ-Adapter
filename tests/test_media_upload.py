import asyncio
import base64
import time

import pytest
from aiohttp import ClientConnectionError

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
    def __init__(self, status: int, session=None) -> None:
        self.status = status
        self._session = session

    async def __aenter__(self):
        if self._session is not None:
            self._session.in_flight += 1
            self._session.max_in_flight = max(self._session.max_in_flight, self._session.in_flight)
            # 让出事件循环，使并发上传的分片能真正重叠，用于断言并发度。
            await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc_info):
        if self._session is not None:
            self._session.in_flight -= 1
        return False


class _FakeContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, size: int):
        for offset in range(0, len(self._body), size):
            yield self._body[offset : offset + size]


class _FakeGet:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content_length = len(body)
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSession:
    """只实现富媒体上传用到的 put 与本机转存取数用到的 get；预签名地址不得携带鉴权头。"""

    def __init__(self, status: int = 200, local_body: bytes = b"", put_failures: int = 0) -> None:
        self.status = status
        self.closed = False
        self.puts = []
        self.local_body = local_body
        self.gets = []
        # 前若干次 PUT 抛网络异常，用于验证按官方 upload_config 下发的策略重试。
        self.put_failures = put_failures
        self.in_flight = 0
        self.max_in_flight = 0

    def put(self, url, **kwargs):
        self.puts.append((url, kwargs))
        if self.put_failures > 0:
            self.put_failures -= 1
            raise ClientConnectionError("connection reset")
        return _FakePut(self.status, self)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return _FakeGet(self.status, self.local_body)


async def _make_plugin(
    parts=None,
    status: int = 200,
    local_body: bytes = b"",
    block_size: int = 0,
    upload_config=None,
    put_failures: int = 0,
):
    """构造绕过 SDK 初始化的插件，捕获 _api_post 与分片 PUT。"""
    plugin = QQOfficialAdapterPlugin.__new__(QQOfficialAdapterPlugin)
    plugin._ready = True
    plugin._id_map = {"group": {}, "user": {}}
    plugin._ref_idx = {}
    plugin._replies = {}
    plugin._reply_lock = asyncio.Lock()
    plugin._ctx = _Ctx()
    plugin._replies[("group", GROUP_OPENID, REPLY_ID)] = (time.time(), 0)
    session = _FakeSession(status=status, local_body=local_body, put_failures=put_failures)
    plugin._session = session
    captured = []
    upload_parts = parts if parts is not None else [{"index": 1, "presigned_url": "https://upload.example/1"}]
    fake_block_size = block_size or len(PNG)

    async def _fake_api_post(path, payload):
        captured.append((path, dict(payload)))
        if path.endswith("/upload_prepare"):
            prepare = {
                "upload_id": "UPLOAD1",
                "block_size": str(fake_block_size),
                "parts": upload_parts,
            }
            if upload_config is not None:
                prepare["upload_config"] = upload_config
            return prepare
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8001/api/files/transfers/token1",
        "http://localhost:8001/api/files/transfers/token1",
        "http://192.168.1.20:8001/api/files/transfers/token1",
        "http://[::1]:8001/api/files/transfers/token1",
    ],
)
async def test_local_transfer_url_is_downloaded_and_uploaded_in_parts(url) -> None:
    # 宿主的附件转存地址默认是本机回环地址，平台抓不到，必须自己取回字节再分片上传。
    pdf = b"%PDF-1.7 local report"
    plugin, captured, session = await _make_plugin(local_body=pdf, block_size=1024)
    message = _media_message({"type": "file", "data": {"name": "report.pdf", "url": url}})
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert [requested for requested, _ in session.gets] == [url]
    files_payload = _post_payload(captured, "/files")
    assert files_payload["file_type"] == 4
    assert files_payload["file_name"] == "report.pdf"
    # 走的是分片合并路径，不能把本机地址交给平台转存。
    assert "url" not in files_payload
    assert files_payload["upload_id"] == "UPLOAD1"
    assert _post_payload(captured, "/upload_prepare")["file_size"] == str(len(pdf))
    assert session.puts[0][1]["data"] == pdf


@pytest.mark.asyncio
async def test_public_url_is_still_transferred_by_platform() -> None:
    # 公网地址仍交给平台转存，避免插件替任意地址做下载代理。
    plugin, captured, session = await _make_plugin()
    message = _media_message(
        {"type": "file", "data": {"name": "report.pdf", "url": "http://example.com/report.pdf"}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert session.gets == []
    assert not [path for path, _ in captured if path.endswith("/upload_prepare")]
    assert _post_payload(captured, "/files")["url"] == "http://example.com/report.pdf"


@pytest.mark.asyncio
async def test_local_transfer_download_failure_reports_error() -> None:
    # 取回本机附件失败时要给出可读错误，且不发起任何 QQ 接口调用。
    plugin, captured, _ = await _make_plugin(status=500, local_body=b"")
    message = _media_message(
        {"type": "file", "data": {"name": "report.pdf", "url": "http://127.0.0.1:8001/api/files/transfers/t1"}}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is False
    assert "宿主附件转存失败: HTTP 500" in result["error"]
    assert captured == []


@pytest.mark.asyncio
async def test_parts_uploaded_concurrently_per_upload_config() -> None:
    # 官方在 upload_config 里下发 concurrency；分片应按该并发度并行上传而不是串行。
    parts = [
        {"index": 1, "presigned_url": "https://upload.example/1"},
        {"index": 2, "presigned_url": "https://upload.example/2"},
        {"index": 3, "presigned_url": "https://upload.example/3"},
    ]
    plugin, captured, session = await _make_plugin(
        parts=parts, block_size=8, upload_config={"concurrency": 3}
    )
    message = _media_message(
        {"type": "image", "data": "", "binary_data_base64": base64.b64encode(PNG + PNG + PNG).decode()}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert len(session.puts) == 3
    # 三个分片同时进入 PUT，说明受 upload_config.concurrency 控制而非串行。
    assert session.max_in_flight == 3


@pytest.mark.asyncio
async def test_upload_concurrency_is_capped() -> None:
    # 平台若下发超出上限的并发度，按本地上限收敛，避免同时打开过多连接。
    parts = [
        {"index": index, "presigned_url": f"https://upload.example/{index}"} for index in range(1, 7)
    ]
    plugin, captured, session = await _make_plugin(
        parts=parts, block_size=8, upload_config={"concurrency": 99}
    )
    message = _media_message(
        {"type": "image", "data": "", "binary_data_base64": base64.b64encode(PNG * 6).decode()}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    assert len(session.puts) == 6
    # 6 个分片并发提交，实际同时在途的数量被压到 4。
    assert session.max_in_flight == 4


@pytest.mark.asyncio
async def test_part_put_retries_on_network_error() -> None:
    # 分片 PUT 遇到网络错误（非平台拒绝）时按重试策略重试，最终成功。
    plugin, captured, session = await _make_plugin(
        put_failures=1, upload_config={"retry_timeout": 5, "retry_delay": 0}
    )
    message = _media_message(
        {"type": "image", "data": "", "binary_data_base64": base64.b64encode(PNG).decode()}
    )
    result = await plugin.handle_qq_official_gateway(message)
    assert result["success"] is True
    # 第一次抛连接错误，重试后成功落片。
    assert len(session.puts) == 2
    assert _post_payload(captured, "/messages")["media"]["file_info"] == FILE_INFO
