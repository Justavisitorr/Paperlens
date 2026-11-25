"""Small local downloads verify durable resume without touching real model caches."""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.download_resource import download_resource

ROOT = Path(__file__).resolve().parents[1]
DATA = bytes(range(256)) * 4096
SHA256 = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def resource_server():
    state = SimpleNamespace(requests=[], mode="range", interrupt=False, delay=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requested = self.headers.get("Range")
            state.requests.append(requested)
            offset = int(requested.removeprefix("bytes=").removesuffix("-")) if requested else 0
            resume = requested and state.mode != "ignore"
            if not resume:
                offset = 0
            self.send_response(206 if resume else 200)
            self.send_header("Content-Length", str(len(DATA) - offset))
            if resume:
                claimed_offset = offset + 1 if state.mode == "invalid" else offset
                self.send_header(
                    "Content-Range", f"bytes {claimed_offset}-{len(DATA) - 1}/{len(DATA)}"
                )
            self.end_headers()
            if state.interrupt:
                state.interrupt = False
                self.wfile.write(DATA[offset : offset + 256 * 1024])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            else:
                try:
                    for start in range(offset, len(DATA), 64 * 1024):
                        self.wfile.write(DATA[start : start + 64 * 1024])
                        self.wfile.flush()
                        if state.delay:
                            time.sleep(state.delay)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    state.url = f"http://127.0.0.1:{server.server_port}/model.onnx"
    yield state
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)


def process_command(server, target):
    script = (
        "from pathlib import Path; from tools.download_resource import download_resource; "
        "import json,sys; a=json.loads(sys.argv[1]); "
        "download_resource(a[0],Path(a[1]),a[2],a[3])"
    )
    return [sys.executable, "-c", script, json.dumps([server.url, str(target), len(DATA), SHA256])]


def invoke_process(server, target):
    return subprocess.run(
        process_command(server, target),
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_new_process_resumes_interrupted_transfer(resource_server, tmp_path):
    target = tmp_path / "model.onnx"
    resource_server.interrupt = True
    first = invoke_process(resource_server, target)
    assert first.returncode != 0
    pending = target.with_suffix(".onnx.part")
    offset = pending.stat().st_size
    assert 0 < offset < len(DATA)
    assert not target.exists()
    second = invoke_process(resource_server, target)
    assert second.returncode == 0, second.stderr
    assert resource_server.requests == [None, f"bytes={offset}-"]
    assert target.read_bytes() == DATA
    assert not pending.exists()


def test_killed_process_releases_lock_and_resumes(resource_server, tmp_path):
    target = tmp_path / "model.onnx"
    pending = target.with_suffix(".onnx.part")
    resource_server.delay = 0.05
    process = subprocess.Popen(
        process_command(resource_server, target),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pending.exists() or pending.stat().st_size < 128 * 1024:
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail("Download process did not retain an incomplete transfer")
            time.sleep(0.01)
        process.kill()
        process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    offset = pending.stat().st_size
    resource_server.delay = 0
    second = invoke_process(resource_server, target)
    assert second.returncode == 0, second.stderr
    assert resource_server.requests == [None, f"bytes={offset}-"]
    assert target.read_bytes() == DATA


def test_server_ignoring_range_restarts_safely(resource_server, tmp_path):
    target = tmp_path / "model.onnx"
    target.with_suffix(".onnx.part").write_bytes(DATA[:1000])
    resource_server.mode = "ignore"
    download_resource(resource_server.url, target, len(DATA), SHA256)
    assert resource_server.requests == ["bytes=1000-"]
    assert target.read_bytes() == DATA


def test_bad_content_range_preserves_prefix(resource_server, tmp_path):
    target = tmp_path / "model.onnx"
    pending = target.with_suffix(".onnx.part")
    pending.write_bytes(DATA[:1000])
    resource_server.mode = "invalid"
    with pytest.raises(ValueError, match="Invalid download range"):
        download_resource(resource_server.url, target, len(DATA), SHA256)
    assert pending.read_bytes() == DATA[:1000]
    assert not target.exists()


@pytest.mark.parametrize("partial_size", [1000, len(DATA), len(DATA) + 1])
def test_corrupt_partial_redownloads(resource_server, tmp_path, partial_size):
    target = tmp_path / "model.onnx"
    target.with_suffix(".onnx.part").write_bytes(b"!" * partial_size)
    download_resource(resource_server.url, target, len(DATA), SHA256)
    assert target.read_bytes() == DATA
    expected = [f"bytes={partial_size}-", None] if partial_size < len(DATA) else [None]
    assert resource_server.requests == expected


def test_verified_final_reused_without_network(resource_server, tmp_path):
    target = tmp_path / "model.onnx"
    target.write_bytes(DATA)
    before = target.stat().st_mtime_ns
    download_resource(resource_server.url, target, len(DATA), SHA256)
    assert target.stat().st_mtime_ns == before
    assert resource_server.requests == []


def test_changed_expected_identity_discards_previous_partial(resource_server, tmp_path):
    target = tmp_path / "model.onnx"
    pending = target.with_suffix(".onnx.part")
    pending.write_bytes(b"old content")
    pending.with_suffix(".part.json").write_text(
        json.dumps({"bytes": len(DATA), "sha256": "different"}), encoding="utf-8"
    )
    download_resource(resource_server.url, target, len(DATA), SHA256)
    assert resource_server.requests == [None]
    assert target.read_bytes() == DATA
