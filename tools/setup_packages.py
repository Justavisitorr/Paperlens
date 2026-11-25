"""Let uv resolve packages, then persist resumable artifacts before installation."""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

try:
    from .download_resource import download_resource
except ImportError:  # Direct execution by the Windows launcher.
    from download_resource import download_resource


def toml_value(value) -> str:
    """Serialize uv's parsed TOML without discarding optional lock metadata."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{toml_value(key)} = {toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise ValueError(f"Unsupported lock metadata type: {type(value).__name__}")


def write_lock(path: Path, lock: dict) -> None:
    text = "\n".join(f"{toml_value(key)} = {toml_value(value)}" for key, value in lock.items())
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(path)


def target_wheel_tags(python: Path) -> list[str]:
    # The launcher bootstraps pip first. Reuse its standard packaging tags for
    # the actual target interpreter instead of maintaining platform rules here.
    output = subprocess.check_output([
        str(python.resolve()), "-c",
        "import json; from pip._vendor.packaging.tags import sys_tags; "
        "print(json.dumps([str(tag) for tag in sys_tags()]))",
    ], text=True)
    return json.loads(output)


def select_wheel(wheels: list[dict], tags: list[str]) -> dict:
    priorities = {tag: rank for rank, tag in enumerate(tags)}
    ranked = []
    for position, wheel in enumerate(wheels):
        filename = unquote(urlsplit(wheel.get("url", wheel.get("path", ""))).path.rsplit("/", 1)[-1])
        parts = filename.removesuffix(".whl").rsplit("-", 3)
        if not filename.endswith(".whl") or len(parts) != 4:
            raise ValueError("Invalid wheel filename in dependency lock.")
        matches = [
            priorities[tag] for py in parts[1].split(".") for abi in parts[2].split(".")
            for platform in parts[3].split(".") if (tag := f"{py}-{abi}-{platform}") in priorities
        ]
        if matches:
            ranked.append((min(matches), position))
    if not ranked:
        raise ValueError("No wheel matches the target Python interpreter.")
    return wheels[min(ranked)[1]]


def artifact_size(url: str) -> int:
    # Some secondary indexes (notably PyTorch) do not publish artifact sizes.
    # HEAD follows redirects and reads no wheel payload.
    with urlopen(Request(url, method="HEAD", headers={"Accept-Encoding": "identity"}), timeout=30) as response:
        value = response.headers.get("Content-Length", "")
    if not value.isdecimal():
        raise ValueError("Package server did not report the artifact size.")
    return int(value)


def prefetch_lock(lock: dict, downloads: Path, tags: list[str] | None = None) -> dict:
    """uv's platform-specific lock chooses the artifact; no dependency resolution here."""
    for package in lock["packages"]:
        wheels = package.get("wheels", [])
        if wheels:
            if len(wheels) > 1:
                if tags is None:
                    tags = target_wheel_tags(Path(sys.executable))
                artifact = select_wheel(wheels, tags)
            else:
                artifact = wheels[0]
        elif "sdist" in package:
            artifact = package["sdist"]
        elif any(key in package for key in ("directory", "vcs", "archive")):
            # Editable projects still use uv's normal build isolation. Its small
            # build dependencies may restart a download after interruption.
            continue
        else:
            raise ValueError(f"No downloadable artifact for {package['name']}.")
        url = artifact.get("url")
        if not url:
            # Already-local artifacts from find-links/path requirements stay local.
            if "path" in artifact:
                continue
            raise ValueError(f"Missing artifact URL for {package['name']}.")
        sha256 = artifact.get("hashes", {}).get("sha256", "")
        size = artifact.get("size")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ValueError(f"Missing SHA-256 for {package['name']}.")
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if not filename or filename in {".", ".."} or any(c in filename for c in '/\\:\x00'):
            raise ValueError(f"Invalid artifact filename for {package['name']}.")
        target = downloads / sha256.lower() / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if size is None:
            size = artifact_size(url)
            artifact["size"] = size
        if type(size) is not int or size < 0:
            raise ValueError(f"Invalid artifact size for {package['name']}.")
        print(f"准备组件：{package['name']} {package.get('version', '')}", flush=True)
        download_resource(url, target, size, sha256)
        artifact.pop("url")
        artifact["path"] = target.resolve().as_posix()
        if wheels:
            # Keep the exact selected wheel; do not allow an unprefetched sdist
            # to become a second download path during installation.
            package.pop("sdist", None)
            package["wheels"] = [artifact]
    return lock


def install_packages(
    uv: Path, python: Path, requirements: Path, root: Path,
    index: str, extra_indexes: list[str],
) -> None:
    root = root.resolve()
    state = root / ".cache/launcher"
    state.mkdir(parents=True, exist_ok=True)
    lock_path = state / "pylock.packages.toml"
    local_lock = state / "pylock.packages-local.toml"
    index_options = ["--default-index", index, "--index-strategy", "unsafe-best-match"]
    for extra in extra_indexes:
        index_options.extend(["--index", extra])
    common = [str(uv.resolve()), "--no-config", "pip"]
    print("正在解析组件版本与下载地址…", flush=True)
    subprocess.run([
        *common, "compile", str(requirements.resolve()), "--format", "pylock.toml",
        "--python", str(python.resolve()), "--python-platform", "windows",
        "--output-file", str(lock_path), "--quiet", *index_options,
    ], cwd=root, check=True)
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    prefetch_lock(lock, state / "downloads/packages", target_wheel_tags(python))
    write_lock(local_lock, lock)
    print("组件下载已校验，正在安装…", flush=True)
    # install preserves existing optional OCR/CUDA components. Build isolation
    # remains online for editable projects and source-only distributions.
    subprocess.run([
        *common, "install", "--python", str(python.resolve()),
        "--requirements", str(local_lock), *index_options,
    ], cwd=root, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--index", "--index-url", default="https://pypi.org/simple")
    parser.add_argument("--extra-index", "--extra-index-url", action="append", default=[])
    args = parser.parse_args()
    try:
        install_packages(args.uv, args.python, args.requirements, args.root, args.index, args.extra_index)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"组件安装未完成；缓存和下载进度已保留。{exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
