"""Model source fallback keeps locked content and partials across trusted endpoints."""

from __future__ import annotations

import hashlib
import io
import json
from urllib.error import URLError
from urllib.request import Request

import pytest

from tools import download_resource as resources

DATA = b"locked model data\n" * 8192
SHA256 = hashlib.sha256(DATA).hexdigest()


def item():
    return {
        "name": "config.json",
        "bytes": len(DATA),
        "sha256": SHA256,
        "url": "https://huggingface.co/test/model/resolve/fixed-commit/config.json",
    }


class Response(io.BytesIO):
    def __init__(self, body, *, offset=0):
        super().__init__(body)
        self.status = 206 if offset else 200
        self.headers = {"Content-Range": f"bytes {offset}-{len(DATA) - 1}/{len(DATA)}"}

    def geturl(self):
        return "https://hf-mirror.com/api/resolve-cache/models/test/model/fixed-commit/config.json"


@pytest.mark.parametrize("saved", ["auto", "china", "official"])
def test_saved_model_policy(tmp_path, saved):
    state = tmp_path / ".cache/launcher/network.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"download_source": saved}), encoding="utf-8-sig")
    assert resources.read_download_source(tmp_path) == saved


@pytest.mark.parametrize("raw", ["[]", "null", '{"download_source":"invalid"}', "bad json"])
def test_invalid_saved_policy_uses_auto(tmp_path, raw):
    state = tmp_path / ".cache/launcher/network.json"
    state.parent.mkdir(parents=True)
    state.write_text(raw, encoding="utf-8")
    assert resources.read_download_source(tmp_path) == "auto"


def test_official_timeout_switches_to_mirror_and_keeps_successful_source(tmp_path, monkeypatch):
    calls = []

    def open_download(request, timeout):
        calls.append(request.full_url)
        assert timeout == 30
        if request.host == "huggingface.co":
            raise URLError(TimeoutError("timed out"))
        return Response(DATA)

    monkeypatch.setattr(resources, "_open_download", open_download)
    for name in ("first.json", "second.json"):
        target = tmp_path / name
        resources.download_hf_file(target, item(), tmp_path)
        assert target.read_bytes() == DATA
    assert [Request(url).host for url in calls] == [
        "huggingface.co",
        "hf-mirror.com",
        "hf-mirror.com",
    ]


def test_saved_china_preference_is_used_without_cli_override(tmp_path, monkeypatch):
    state = tmp_path / ".cache/launcher/network.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"download_source":"china"}', encoding="utf-8")
    calls = []

    def open_download(request, timeout):
        calls.append(request.host)
        return Response(DATA)

    monkeypatch.setattr(resources, "_open_download", open_download)
    resources.download_hf_file(tmp_path / "config.json", item(), tmp_path)
    assert calls == ["hf-mirror.com"]


def test_official_policy_does_not_try_mirror_and_reports_friendly_error(tmp_path, monkeypatch):
    calls = []

    def timeout(request, **kwargs):
        calls.append(request.host)
        raise URLError(TimeoutError("timed out"))

    monkeypatch.setattr(resources, "_open_download", timeout)
    with pytest.raises(RuntimeError, match="已保留已下载文件和续传进度") as error:
        resources.download_hf_file(tmp_path / "config.json", item(), tmp_path, "official")
    assert "超时" in str(error.value)
    assert calls == ["huggingface.co"]


def test_partial_transfer_continues_on_mirror(tmp_path, monkeypatch):
    calls = []
    retained = 64 * 1024

    def open_download(request, timeout):
        calls.append((request.host, request.get_header("Range")))
        if request.host == "huggingface.co":
            return Response(DATA[:retained])
        return Response(DATA[retained:], offset=retained)

    monkeypatch.setattr(resources, "_open_download", open_download)
    target = tmp_path / "config.json"
    resources.download_hf_file(target, item(), tmp_path)
    assert calls == [("huggingface.co", None), ("hf-mirror.com", f"bytes={retained}-")]
    assert target.read_bytes() == DATA
    assert not target.with_suffix(".json.part").exists()
