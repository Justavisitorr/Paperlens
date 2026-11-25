"""Release-verified downloads with persistent partial files and HTTP Range support."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

DOWNLOAD_TIMEOUT = 30
_HF_WORKING_SOURCE: dict[tuple[Path, str], str] = {}


def _open_download(request: Request, timeout: int):
    return urlopen(request, timeout=timeout)


def read_download_source(root: Path) -> str:
    """Use the installer's saved policy for standalone model setup too."""
    try:
        saved = json.loads((root / ".cache/launcher/network.json").read_text("utf-8-sig"))
        source = saved.get("download_source")
        if source in {"auto", "china", "official"}:
            return source
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return "auto"


def _download_error(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"服务器返回 HTTP {exc.code}"
    reason = getattr(exc, "reason", exc)
    if (
        isinstance(reason, TimeoutError)
        or "timed out" in str(reason).lower()
        or "10060" in str(reason)
    ):
        return f"连接或读取超时（每次最多等待 {DOWNLOAD_TIMEOUT} 秒）"
    return f"连接、文件读取或校验失败：{reason}"


def download_hf_file(
    target: Path,
    item: dict,
    root: Path,
    download_source: str | None = None,
) -> None:
    """Switch pinned Hugging Face artifacts between official and domestic hosts."""
    source = download_source or read_download_source(root)
    orders = {
        "auto": ["official", "china"],
        "china": ["china", "official"],
        "official": ["official"],
    }
    if source not in orders:
        raise ValueError("下载源必须为 auto、china 或 official。")
    original = urlsplit(item["url"])
    if original.scheme != "https" or original.netloc != "huggingface.co":
        raise ValueError("模型地址不是锁定的 Hugging Face 官方地址。")
    # Avoid waiting for the same blocked endpoint before every small model file.
    key = (root.resolve(), source)
    order = list(orders[source])
    preferred = _HF_WORKING_SOURCE.get(key)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    failures = []
    for candidate in order:
        host = "huggingface.co" if candidate == "official" else "hf-mirror.com"
        label = "官方" if candidate == "official" else "国内镜像"
        url = urlunsplit(original._replace(netloc=host))
        print(f"模型下载源：{label}（{host}）；文件：{item['name']}", flush=True)
        try:
            download_file(target, {**item, "url": url})
            _HF_WORKING_SOURCE[key] = candidate
            return
        except (OSError, ValueError, http.client.HTTPException) as exc:
            message = _download_error(exc)
            failures.append(f"{host}：{message}")
            print(f"模型下载未完成（{host}）：{message}；已保留可续传内容。", flush=True)
    raise RuntimeError(
        "模型下载未完成，已保留已下载文件和续传进度。"
        "请在安装器切换下载来源，或使用 --download-source china 重试。" + "；".join(failures)
    )


def verify_file(path: Path, item: dict) -> None:
    if not path.is_file() or path.stat().st_size != item["bytes"]:
        raise ValueError(f"Missing or incorrect resource size: {item['name']}")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != item["sha256"]:
        raise ValueError(f"Resource checksum mismatch: {item['name']}")


@contextmanager
def _download_lock(target: Path):
    """OS-held locks release automatically if a setup process is killed."""
    with target.with_suffix(target.suffix + ".part.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Another setup process is downloading {target.name}") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def download_file(target: Path, item: dict) -> None:
    """Resume into a stable .part file; replace the target only after verification."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with _download_lock(target):
        _download_file(target, item)


def download_resource(url: str, target: Path, size: int, sha256: str) -> None:
    """Download a fixed artifact using only the standard library."""
    target = Path(target)
    download_file(target, {"url": url, "name": target.name, "bytes": size, "sha256": sha256})


def _download_file(target: Path, item: dict) -> None:
    """Keep a partial download across runs; publish only release-verified bytes."""
    if target.is_file():
        try:
            verify_file(target, item)
            return
        except ValueError:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_suffix(target.suffix + ".part")
    metadata = pending.with_suffix(pending.suffix + ".json")
    identity = {"bytes": item["bytes"], "sha256": item["sha256"]}
    if metadata.is_file():
        try:
            previous_identity = json.loads(metadata.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            previous_identity = None
        if previous_identity != identity:
            pending.unlink(missing_ok=True)
    metadata.write_text(json.dumps(identity), encoding="utf-8")
    for attempt in range(2):
        offset = pending.stat().st_size if pending.is_file() else 0
        if offset >= item["bytes"]:
            try:
                verify_file(pending, item)
                pending.replace(target)
                metadata.unlink(missing_ok=True)
                return
            except ValueError:
                pending.unlink()
                offset = 0
        headers = {"Accept-Encoding": "identity", "User-Agent": "PaperLens-Setup"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        print(
            f"下载 {item['name']}（{urlsplit(item['url']).hostname}）："
            f"{offset:,}/{item['bytes']:,} 字节",
            flush=True,
        )
        with _open_download(
            Request(item["url"], headers=headers), timeout=DOWNLOAD_TIMEOUT
        ) as response:
            final_host = urlsplit(response.geturl()).hostname
            if final_host != urlsplit(item["url"]).hostname:
                print(f"实际文件下载服务器：{final_host}", flush=True)
            if response.status == 206:
                match = re.fullmatch(
                    r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                )
                if not match or tuple(map(int, match.groups())) != (
                    offset,
                    item["bytes"] - 1,
                    item["bytes"],
                ):
                    raise ValueError(f"Invalid download range: {item['name']}")
                mode = "ab" if offset else "wb"
            elif response.status == 200:
                # Servers may ignore Range. Truncate before accepting the full body.
                mode = "wb"
            else:
                raise ValueError(f"Unexpected download status: {response.status}")
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError(f"Unexpected download encoding: {item['name']}")
            last_report = time.monotonic()
            with pending.open(mode) as handle:
                while chunk := response.read(64 * 1024):
                    handle.write(chunk)
                    if handle.tell() > item["bytes"]:
                        raise ValueError(f"Download exceeds release size: {item['name']}")
                    if time.monotonic() - last_report >= 3:
                        print(
                            f"下载 {item['name']}：{handle.tell():,}/{item['bytes']:,} 字节",
                            flush=True,
                        )
                        last_report = time.monotonic()
        try:
            verify_file(pending, item)
        except ValueError:
            # A complete but corrupt prefix cannot be resumed. Retry once from zero.
            if pending.stat().st_size >= item["bytes"]:
                pending.unlink()
                if attempt == 0:
                    continue
            raise
        pending.replace(target)
        metadata.unlink(missing_ok=True)
        return
