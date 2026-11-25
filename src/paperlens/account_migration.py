"""Portable, password-encrypted account bundles. Source and destination stay separate."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from dotenv import dotenv_values
from fastapi import HTTPException

from .account_store import MAX_SYNC_BYTES, MAX_SYNC_KEYS, sync_key
from .config import Settings
from .storage import write_json

MAGIC = b"PAPERLENS-BACKUP-1\n"
MAX_ARCHIVE = 2 * 1024**3
MAX_EXPANDED = 4 * 1024**3
MAX_FILES = 100000
CHUNK = 1024**2


def validate_password(password: str):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise HTTPException(422, "迁移包密码应为 12–128 个字符。请与账户密码分开保存。")


def password_key(password: str, salt: bytes):
    validate_password(password)
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())


def browser_data(value):
    if not isinstance(value, dict) or len(value) > MAX_SYNC_KEYS:
        raise ValueError("浏览器资料格式无效。")
    if any(not sync_key(key) or not isinstance(item, str) or len(item.encode()) > CHUNK
           for key, item in value.items()):
        raise ValueError("浏览器资料包含不支持的字段。")
    if sum(len(v.encode()) for v in value.values()) > MAX_SYNC_BYTES:
        raise ValueError("浏览器资料过大。")
    return value


def encrypt_file(source: Path, target: Path, password: str):
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    encryptor = Cipher(algorithms.AES(password_key(password, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with source.open("rb") as src, target.open("xb") as dst:
        dst.write(header)
        while chunk := src.read(CHUNK):
            dst.write(encryptor.update(chunk))
        dst.write(encryptor.finalize())
        dst.write(encryptor.tag)


def decrypt_file(source: Path, target: Path, password: str):
    size = source.stat().st_size
    header_size = len(MAGIC) + 28
    if size > MAX_ARCHIVE or size < header_size + 16:
        raise ValueError("资料包大小无效或超过 2 GB。")
    with source.open("rb") as src, target.open("xb") as dst:
        header = src.read(header_size)
        if not header.startswith(MAGIC):
            raise ValueError("这不是 PaperLens 加密资料包。")
        src.seek(-16, 2)
        tag = src.read(16)
        src.seek(header_size)
        decryptor = Cipher(algorithms.AES(password_key(password, header[len(MAGIC):len(MAGIC) + 16])), modes.GCM(header[-12:], tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining = size - header_size - 16
        while remaining:
            chunk = src.read(min(CHUNK, remaining))
            if not chunk:
                raise ValueError("资料包不完整。")
            dst.write(decryptor.update(chunk))
            remaining -= len(chunk)
        try:
            dst.write(decryptor.finalize())
        except InvalidTag as exc:
            raise ValueError("密码不正确，或资料包已损坏。") from exc


def export_bundle(workspace: Path, policy, browser: dict, password: str, destination: Path):
    validate_password(password)
    browser_data(browser)
    if not policy.credential_key:
        raise ValueError("工作区加密信息不可用。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pl-export-", dir=destination.parent) as temp:
        archive = Path(temp) / "payload.zip"
        manifest = {"version": 1, "credential_key": policy.credential_key.decode("ascii"), "files": {}}
        total = 0
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
            for name in ("outputs", "cache", ".paperlens"):
                base = workspace / name
                if base.is_symlink():
                    raise ValueError("工作区含不支持的链接目录。")
                if not base.exists():
                    continue
                for folder, dirs, names in os.walk(base, followlinks=False):
                    if any((Path(folder) / d).is_symlink() for d in dirs):
                        raise ValueError("工作区含不支持的链接目录。")
                    for filename in names:
                        path = Path(folder) / filename
                        if path.is_symlink() or not path.resolve().is_relative_to(workspace.resolve()):
                            raise ValueError("工作区含不支持的链接文件。")
                        before = path.stat()
                        total += before.st_size
                        if total > MAX_EXPANDED or len(manifest["files"]) >= MAX_FILES:
                            raise ValueError("资料超出网页迁移上限，请使用服务器整盘备份。")
                        relative = "workspace/" + path.relative_to(workspace).as_posix()
                        digest = hashlib.sha256()
                        with path.open("rb") as src, bundle.open(relative, "w", force_zip64=True) as dst:
                            while chunk := src.read(CHUNK):
                                digest.update(chunk)
                                dst.write(chunk)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError("资料在导出过程中变化，请停止其他操作后重试。")
                        manifest["files"][relative] = {"size": before.st_size, "sha256": digest.hexdigest()}
            bundle.writestr("browser.json", json.dumps(browser, ensure_ascii=False))
            bundle.writestr("manifest.json", json.dumps(manifest))
        if archive.stat().st_size + len(MAGIC) + 44 > MAX_ARCHIVE:
            raise ValueError("压缩资料包超过 2 GB，请使用服务器整盘备份。")
        encrypt_file(archive, destination, password)


def _safe_members(bundle):
    seen, total = set(), 0
    infos = bundle.infolist()
    if len(infos) > MAX_FILES + 2:
        raise ValueError("资料包文件数过多。")
    for info in infos:
        path = PurePosixPath(info.filename)
        normalized = info.filename.casefold()
        if (info.is_dir() or "\\" in info.filename or ":" in info.filename or path.is_absolute()
                or path.as_posix() != info.filename
                or any(part in {"", ".", ".."} or part.rstrip(". ") != part for part in path.parts)
                or any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path.parts)
                or normalized in seen or stat.S_ISLNK(info.external_attr >> 16)
                or (info.filename not in {"manifest.json", "browser.json"}
                    and (len(path.parts) < 3 or path.parts[0] != "workspace" or path.parts[1] not in {"cache", "outputs", ".paperlens"}))):
            raise ValueError("资料包包含无效或重复路径。")
        seen.add(normalized)
        total += info.file_size
        if total > MAX_EXPANDED or info.file_size > MAX_EXPANDED:
            raise ValueError("资料包展开后过大。")
    return infos


def unpack_bundle(source: Path, password: str, staging: Path, policy):
    """Validate fully into a new directory; the existing workspace is never touched."""
    staging.mkdir(parents=True, mode=0o700)
    archive = staging / "payload.zip"
    decrypt_file(source, archive, password)
    with zipfile.ZipFile(archive) as bundle:
        infos = _safe_members(bundle)
        for name, limit in (("manifest.json", 32 * CHUNK), ("browser.json", 12 * CHUNK)):
            if bundle.getinfo(name).file_size > limit:
                raise ValueError("资料包索引过大。")
        manifest = json.loads(bundle.read("manifest.json"))
        browser = browser_data(json.loads(bundle.read("browser.json")))
        if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
            raise ValueError("不支持的资料包版本。")
        old_cipher = Fernet(manifest["credential_key"].encode("ascii"))
        expected = set(manifest["files"])
        actual = {info.filename for info in infos} - {"manifest.json", "browser.json"}
        if expected != actual:
            raise ValueError("资料包文件清单不一致。")
        for name in sorted(actual):
            target = staging.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(staging.resolve() / "workspace"):
                raise ValueError("资料路径越界。")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with bundle.open(name) as src, target.open("xb") as dst:
                while chunk := src.read(CHUNK):
                    size += len(chunk)
                    if size > manifest["files"][name]["size"]:
                        raise ValueError("资料文件大小校验失败。")
                    digest.update(chunk)
                    dst.write(chunk)
            if size != manifest["files"][name]["size"] or digest.hexdigest() != manifest["files"][name]["sha256"]:
                raise ValueError("资料文件完整性校验失败。")
    workspace = staging / "workspace"
    workspace.mkdir(exist_ok=True)

    def rekey(value):
        return policy.seal(old_cipher.decrypt(value.encode()).decode()) if value else ""

    connection_file = workspace / ".paperlens/connections.json"
    if connection_file.exists():
        connections = json.loads(connection_file.read_text("utf-8"))
        for account in connections["accounts"]:
            account["api_key"] = rekey(account["api_key"])
            policy.check_endpoint(account["base_url"])
        write_json(connection_file, connections)
    for secret_file in (workspace / "cache/runtime-secrets").glob("*.env"):
        values = dotenv_values(stream=io.StringIO(secret_file.read_text("utf-8")), interpolate=False)
        if set(values) != {"LLM_API_KEY"} or values["LLM_API_KEY"] is None:
            raise ValueError("模型凭据文件格式无效。")
        secret_file.write_text("LLM_API_KEY=" + json.dumps(rekey(values["LLM_API_KEY"])) + "\n", encoding="utf-8")
    # Reject stored settings that would become an SSRF route after import.
    settings_file = workspace / "cache/ui-settings.json"
    if settings_file.exists():
        values = json.loads(settings_file.read_text("utf-8"))["values"]
        if values.get("LLM_BASE_URL"):
            policy.check_endpoint(values["LLM_BASE_URL"])
    allowed_job_fields = set(Settings.model_fields) - {"llm_api_key", "paperlens_output_dir", "paperlens_cache_dir"}
    for job_file in (workspace / "cache/jobs").glob("*/*job.json"):
        job = json.loads(job_file.read_text("utf-8"))
        configuration = job.get("configuration", {})
        if not isinstance(configuration, dict) or set(configuration) - allowed_job_fields:
            raise ValueError("任务配置包含不允许迁移的字段。")
        if configuration.get("llm_base_url"):
            policy.check_endpoint(configuration["llm_base_url"])
        for route in configuration.get("llm_routes", {}).values():
            if route.get("api_key"):
                raise ValueError("任务快照不应含模型明文凭据。")
            policy.check_endpoint(route["base_url"])
        relative = job.get("report_relative_path")
        if relative:
            # Windows queue records historically used '\\'; imports also work on Linux.
            portable = relative.replace("\\", "/")
            parts = PurePosixPath(portable)
            if parts.is_absolute() or ".." in parts.parts or ":" in portable:
                raise ValueError("任务报告路径无效。")
            job["report_relative_path"] = portable
            write_json(job_file, job)
    archive.unlink()
    return workspace, browser, len(actual)


def require_empty(workspace: Path, store, user_id: str):
    if (any((workspace / "outputs").glob("**/report.json"))
            or any((workspace / "cache/jobs").glob("*/*.json"))
            or (workspace / ".paperlens/connections.json").exists()
            or (workspace / "cache/ui-settings.json").exists()
            or any((workspace / "outputs/.reader").glob("**/*.json"))):
        raise HTTPException(409, "导入需要空工作区，现有论文或模型配置不会被覆盖。请使用新账户或先导出资料。")
    with store.db() as db:
        if db.execute("SELECT 1 FROM browser_data WHERE user_id=? LIMIT 1", (user_id,)).fetchone():
            raise HTTPException(409, "此账户已有同步资料，请使用空账户导入整包。")


def import_bundle(source: Path, password: str, root: Path, store, user_id: str, policy):
    target = root / "workspaces" / policy.workspace_id
    require_empty(target, store, user_id)
    transaction = root / "migration" / uuid.uuid4().hex
    transaction.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous = transaction / "previous"
    moved = False
    committed = False
    try:
        incoming, browser, count = unpack_bundle(source, password, transaction, policy)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Marker makes a process crash between filesystem and SQLite commit recoverable.
        write_json(transaction / "transaction.json", {"user_id": user_id, "workspace_id": policy.workspace_id})
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM browser_data WHERE user_id=? LIMIT 1", (user_id,)).fetchone():
                raise HTTPException(409, "账户已有同步资料，无法覆盖。")
            if target.exists():
                target.rename(previous)
            incoming.rename(target)
            moved = True
            for key, value in browser.items():
                db.execute("INSERT INTO browser_data VALUES (?,?,?,?,?)", (user_id, key, policy.seal(value), 1, len(value.encode())))
            db.execute("INSERT INTO migrations VALUES (?,?)", (transaction.name, user_id))
        committed = True
        shutil.rmtree(transaction, ignore_errors=True)
        return {"ok": True, "files": count, "browser_items": len(browser)}
    except BaseException:
        if committed:
            raise
        if moved and target.exists():
            shutil.rmtree(target)
        if previous.exists():
            previous.rename(target)
        if transaction.exists():
            shutil.rmtree(transaction)
        raise


def recover_migrations(root: Path, store):
    for marker in (root / "migration").glob("*/transaction.json"):
        record = json.loads(marker.read_text("utf-8"))
        workspace_id = record["workspace_id"]
        if not re.fullmatch("[a-f0-9]{64}", workspace_id):
            raise RuntimeError("迁移恢复索引无效，请检查备份。")
        with store.db() as db:
            committed = db.execute("SELECT 1 FROM migrations WHERE id=?", (marker.parent.name,)).fetchone()
        target = root / "workspaces" / workspace_id
        previous = marker.parent / "previous"
        incoming = marker.parent / "workspace"
        if not committed:
            if target.exists() and not incoming.exists():
                shutil.rmtree(target)
            if previous.exists():
                previous.rename(target)
        shutil.rmtree(marker.parent)
