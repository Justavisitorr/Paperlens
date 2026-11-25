"""Explicit standalone configuration; never consumes the desktop model .env."""

from __future__ import annotations

import base64
import ipaddress
import os
import secrets
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from .storage import write_json
from .web_policy import approved_endpoints
from .web_service import ServiceConfig


@dataclass(frozen=True)
class StandaloneConfig:
    root: Path
    origin: str
    dev_mail: bool = False
    github_id: str = ""
    github_secret: str = field(default="", repr=False)
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_from: str = ""
    smtp_starttls: bool = False
    max_users: int = 100
    processing_slots: int = 2
    extra_endpoints: tuple[str, ...] = ()
    trusted_proxies: str = "127.0.0.1,::1"

    def __post_init__(self):
        url = urlsplit(self.origin)
        loopback = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.path or url.scheme not in {"https", "http"}
                or (url.scheme == "http" and not loopback)):
            raise ValueError("正式服务必须填写 HTTPS 来源地址（不含路径）；HTTP 仅允许本机测试。")
        if self.dev_mail and not loopback:
            raise ValueError("测试邮件仅允许本机地址，不能用于公网。")
        if not 1 <= self.max_users <= 1000 or not 1 <= self.processing_slots <= 8:
            raise ValueError("注册上限为 1–1000，处理并发为 1–8。")
        if bool(self.github_id) != bool(self.github_secret):
            raise ValueError("GitHub Client ID 与 Secret 必须同时配置。")
        if any("\n" in v or "\r" in v for v in (self.smtp_from, self.smtp_user, self.smtp_host)):
            raise ValueError("邮件配置不能含换行符。")
        approved_endpoints(self.extra_endpoints)
        for proxy in filter(None, self.trusted_proxies.split(",")):
            network = ipaddress.ip_network(proxy.strip(), strict=False)
            if network.prefixlen == 0:
                raise ValueError("不能信任所有来源的代理头。请填写实际反向代理 IP。")

    @classmethod
    def from_env(cls, root: Path, origin: str, dev_mail: bool = False, env_file: Path | None = None):
        values = {**(dotenv_values(env_file, interpolate=False) if env_file else {}), **os.environ}

        def env(name, default=""):
            return values.get(name) or default

        def secret(name):
            path = env(name + "_FILE")
            if path:
                filename = Path(path)
                if env_file and not filename.is_absolute():
                    filename = env_file.resolve().parent / filename
                return filename.read_text("utf-8").strip()
            return env(name)

        return cls(
            root=root.resolve(), origin=origin.rstrip("/"), dev_mail=dev_mail,
            github_id=env("PAPERLENS_GITHUB_CLIENT_ID"),
            github_secret=secret("PAPERLENS_GITHUB_CLIENT_SECRET"),
            smtp_host=env("PAPERLENS_SMTP_HOST"),
            smtp_port=int(env("PAPERLENS_SMTP_PORT", "465")),
            smtp_user=env("PAPERLENS_SMTP_USER"),
            smtp_password=secret("PAPERLENS_SMTP_PASSWORD"),
            smtp_from=env("PAPERLENS_SMTP_FROM"),
            smtp_starttls=env("PAPERLENS_SMTP_STARTTLS", "false").lower() == "true",
            max_users=int(env("PAPERLENS_MAX_USERS", "100")),
            processing_slots=int(env("PAPERLENS_PROCESSING_SLOTS", "2")),
            extra_endpoints=tuple(filter(None, env("PAPERLENS_ALLOWED_MODEL_URLS").split(","))),
            trusted_proxies=env("PAPERLENS_TRUSTED_PROXIES", "127.0.0.1,::1"),
        )

    @property
    def secure(self):
        return self.origin.startswith("https://")

    @property
    def email_enabled(self):
        return self.dev_mail or bool(self.smtp_host and self.smtp_from)

    def service(self):
        root = self.root.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (root / "tmp").mkdir(exist_ok=True, mode=0o700)
        key_path = root / "account.key"
        if not key_path.exists():
            if (root / "accounts.sqlite3").exists() or (root / "workspaces").exists():
                raise RuntimeError("账户密钥缺失，请从备份恢复 account.key；不能重新生成。")
            with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
                handle.flush()
                os.fsync(handle.fileno())
        key = base64.urlsafe_b64decode(key_path.read_bytes())
        if len(key) != 32:
            raise ValueError("账户主密钥无效。")
        return ServiceConfig(root, self.origin, b"", key, approved_endpoints(self.extra_endpoints), self.max_users + 1)


def send_code(config: StandaloneConfig, challenge: str, email: str, code: str):
    if config.dev_mail:
        folder = config.root / "test-mail"
        folder.mkdir(mode=0o700, exist_ok=True)
        # Intentionally local-only, never returned through an HTTP endpoint or log.
        write_json(folder / f"{challenge}.json", {"email": email, "code": code})
        return
    message = EmailMessage()
    message["Subject"] = "PaperLens 登录验证码"
    message["From"], message["To"] = config.smtp_from, email
    message.set_content(f"你的 PaperLens 验证码是：{code}\n10 分钟内有效。若非本人操作，请忽略本邮件。")
    context = ssl.create_default_context()
    client = (smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15)
              if config.smtp_starttls else smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=15, context=context))
    with client:
        if config.smtp_starttls:
            client.starttls(context=context)
        if config.smtp_user:
            client.login(config.smtp_user, config.smtp_password)
        client.send_message(message)
