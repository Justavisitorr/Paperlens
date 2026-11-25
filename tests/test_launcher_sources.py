"""Exercise source fallback and durable byte resume without real downloads."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools/install_sources.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="Windows PowerShell required")
PAYLOAD = bytes(range(256)) * 4096
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def script_file(tmp_path, body):
    script = tmp_path / "test.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "$env:PSModulePath = (Join-Path $PSHOME 'Modules') + ';' + $env:PSModulePath\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        f". {quote(HELPER)}\n{body}\n",
        encoding="utf-8-sig",
    )
    return script


def command(script):
    return [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]


def run_ps(tmp_path, body):
    result = subprocess.run(command(script_file(tmp_path, body)), capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.decode("utf-8", errors="replace")


@contextmanager
def server(*, ranges=True, slow=False, reject_range=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            offset = 0
            header = self.headers.get("Range")
            requests.append((self.path, header))
            if reject_range and header:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if ranges and header:
                offset = int(header.split("=")[1].split("-")[0])
            payload = PAYLOAD if self.path != "/bad" else b"invalid wheel"
            self.send_response(206 if offset else 200)
            self.send_header("Content-Length", str(len(payload) - offset))
            if offset:
                self.send_header("Content-Range", f"bytes {offset}-{len(payload) - 1}/{len(payload)}")
            self.end_headers()
            try:
                for start in range(offset, len(payload), 16384):
                    self.wfile.write(payload[start:start + 16384])
                    self.wfile.flush()
                    if slow:
                        time.sleep(0.025)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=3)


def test_source_precedence_and_orders(tmp_path):
    settings = tmp_path / "network.json"
    settings.write_text(json.dumps({"download_source": "china"}), encoding="utf-8")
    output = run_ps(tmp_path, f"""
$values = @(
    (Resolve-DownloadSource 'official' {quote(settings)}),
    (Resolve-DownloadSource '' {quote(settings)}),
    (Resolve-DownloadSource '' {quote(tmp_path / 'missing.json')}),
    ((Get-DownloadSourceOrder 'auto') -join ','),
    ((Get-DownloadSourceOrder 'china') -join ','),
    ((Get-DownloadSourceOrder 'official') -join ',')
)
$values | ConvertTo-Json -Compress
""")
    assert json.loads(output) == ["official", "china", "auto", "official,china", "china,official", "official"]


def test_resume_after_process_tree_is_killed(tmp_path):
    destination = tmp_path / "uv.zip"
    partial = tmp_path / "uv.zip.part"
    with server(slow=True) as (url, requests):
        script = script_file(tmp_path, f"Receive-VerifiedDownload {quote(url)} {quote(destination)} '{SHA256}'")
        process = subprocess.Popen(command(script), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if partial.exists() and 65536 <= partial.stat().st_size < len(PAYLOAD):
                    break
                if process.poll() is not None:
                    pytest.fail(str(process.communicate()))
                time.sleep(0.02)
            else:
                pytest.fail("download did not create a partial file")
            # Killing only the shell would leave its curl child downloading.
            subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=True)
            process.communicate(timeout=10)
            retained = partial.stat().st_size
            assert 0 < retained < len(PAYLOAD)
            run_ps(tmp_path, f"Receive-VerifiedDownload {quote(url)} {quote(destination)} '{SHA256}'")
        finally:
            if process.poll() is None:
                subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                process.communicate(timeout=10)
    assert requests[-1][1] == f"bytes={retained}-"
    assert destination.read_bytes() == PAYLOAD
    assert not partial.exists()


def test_server_without_range_restarts_file_safely(tmp_path):
    destination = tmp_path / "uv.zip"
    (tmp_path / "uv.zip.part").write_bytes(PAYLOAD[:1000])
    with server(ranges=False) as (url, requests):
        run_ps(tmp_path, f"Receive-VerifiedDownload {quote(url)} {quote(destination)} '{SHA256}'")
    assert requests[0][1] == "bytes=1000-"
    assert requests[-1][1] is None
    assert destination.read_bytes() == PAYLOAD


def test_hash_failure_discards_partial_and_falls_back(tmp_path):
    destination = tmp_path / "uv.zip"
    with server() as (url, requests):
        run_ps(tmp_path, f"""
function Get-UvBootstrapUrl([string]$Source, [string]$Version) {{
    if ($Source -eq 'official') {{ return '{url}/bad' }}
    return '{url}/good'
}}
Install-UvBootstrap 'auto' {quote(destination)} '0.12.23' '{SHA256}'
""")
    assert [path for path, _ in requests] == ["/bad", "/good"]
    assert destination.read_bytes() == PAYLOAD


def test_rejected_range_discards_stale_partial_and_restarts(tmp_path):
    destination = tmp_path / "uv.zip"
    (tmp_path / "uv.zip.part").write_bytes(PAYLOAD[:1000])
    with server(reject_range=True) as (url, requests):
        run_ps(tmp_path, f"Receive-VerifiedDownload {quote(url)} {quote(destination)} '{SHA256}'")
    assert requests[0][1] == "bytes=1000-"
    assert requests[-1][1] is None
    assert destination.read_bytes() == PAYLOAD


def test_official_profile_never_tries_mirror_on_failure(tmp_path):
    destination = tmp_path / "uv.zip"
    with server() as (url, requests):
        output = run_ps(tmp_path, f"""
function Get-UvBootstrapUrl([string]$Source, [string]$Version) {{ return '{url}/bad' }}
try {{ Install-UvBootstrap 'official' {quote(destination)} '0.12.23' '{SHA256}'; throw 'unexpected success' }}
catch {{ if ($_.Exception.Message -eq 'unexpected success') {{ throw }}; Write-Output 'expected failure' }}
""")
    assert "expected failure" in output
    assert len(requests) == 1
    assert not destination.exists()
    assert not (tmp_path / "uv.zip.part").exists()


def test_uv_fallback_uses_explicit_index_and_restores_environment(tmp_path):
    output = run_ps(tmp_path, """
$env:UV_DEFAULT_INDEX = 'https://old.invalid/simple'
$calls = New-Object System.Collections.ArrayList
function Test-Uv {
    $null = $calls.Add(@{ arguments = @($args); inherited = $env:UV_DEFAULT_INDEX })
    $global:LASTEXITCODE = if ($args -contains 'https://pypi.org/simple') { 1 } else { 0 }
}
Invoke-UvSourceStep -Uv 'Test-Uv' -Arguments @('pip', 'install', 'example==1.0') -Profile 'auto' -Stage 'dependency'
@{ calls = @($calls); restored = $env:UV_DEFAULT_INDEX } | ConvertTo-Json -Depth 8 -Compress
""")
    result = json.loads(output.strip().splitlines()[-1])
    assert result["restored"] == "https://old.invalid/simple"
    assert len(result["calls"]) == 2
    assert all(call["inherited"] is None for call in result["calls"])
    assert "https://pypi.org/simple" in result["calls"][0]["arguments"]
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in result["calls"][1]["arguments"]
    assert "unsafe-best-match" in result["calls"][1]["arguments"]


def test_python_prefetch_uses_uv_local_mirror_validation(tmp_path):
    # uv's real checksum is tested by the isolated installation smoke test. Here
    # require our caller to hand validation/install to uv, and retry a rejected
    # cached archive rather than extracting it in our own code.
    archive = tmp_path / "downloads/python/20261003/python.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"corrupt cached archive")
    output = run_ps(tmp_path, f"""
$calls = New-Object System.Collections.ArrayList
$env:UV_PYTHON_DOWNLOADS_JSON_URL = 'original-custom-metadata'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
function Receive-VerifiedDownload {{
    param($Url, $Destination, $Sha256, [switch]$UvPythonArchive)
    if (-not (Test-Path -LiteralPath $Destination)) {{ [IO.File]::WriteAllText($Destination, 'valid archive') }}
}}
function Test-Uv {{
    $global:LASTEXITCODE = 0
    if ($args -contains '--only-installed') {{
        if ($args -contains '--managed-python') {{ throw 'Conflicting managed Python arguments' }}
        if ($args -notcontains '--python-preference') {{ throw 'Managed Python preference missing' }}
        return '[]'
    }}
    if ($args -contains '--only-downloads') {{ return '[{{"key":"cpython-3.12.15-windows-x86_64-none","url":"https://releases.astral.sh/github/python-build-standalone/releases/download/20261003/python.tar.gz","implementation":"cpython","os":"windows","arch":"x86_64","variant":"default"}}]' }}
    $null = $calls.Add(@($args))
    if ([IO.File]::ReadAllText({quote(archive)}) -ne 'valid archive') {{ $global:LASTEXITCODE = 1 }}
}}
Install-ManagedPython 'Test-Uv' 'china' {quote(tmp_path)}
@{{ calls = @($calls); restored = $env:UV_PYTHON_DOWNLOADS_JSON_URL }} | ConvertTo-Json -Depth 8 -Compress
""")
    result = json.loads(output.strip().splitlines()[-1])
    assert len(result["calls"]) == 2
    for call in result["calls"]:
        assert "--no-registry" in call and "--no-bin" in call
        assert "cpython-3.12.15-windows-x86_64-none" in call
        assert call[call.index("--mirror") + 1].startswith("file:///")
    assert result["restored"] == "original-custom-metadata"
    assert archive.read_text() == "valid archive"


def test_package_wrapper_receives_source_flags_without_uv_cli_flags(tmp_path):
    output = run_ps(tmp_path, """
$calls = New-Object System.Collections.ArrayList
function Test-Python {
    $null = $calls.Add(@($args))
    $global:LASTEXITCODE = 0
}
Invoke-UvSourceStep -Uv 'Test-Python' -Arguments @('tools/setup_packages.py', '--requirements', 'requirements.txt') -Profile 'china' -Stage 'dependency' -PackageInstall
@{ calls = @($calls) } | ConvertTo-Json -Depth 8 -Compress
""")
    result = json.loads(output.strip().splitlines()[-1])
    assert result["calls"] == [["tools/setup_packages.py", "--requirements", "requirements.txt", "--index", "https://pypi.tuna.tsinghua.edu.cn/simple"]]
