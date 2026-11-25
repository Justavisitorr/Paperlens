"""Server-owned limits and credentials for one authenticated PaperLens workspace."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from importlib.resources import files
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken


def approved_endpoints(extra: tuple[str, ...] = ()) -> frozenset[str]:
    catalog = json.loads(
        files("paperlens").joinpath("assets/service-presets.json").read_text("utf-8")
    )
    urls = {
        item["base_url"] for item in catalog["providers"] if item["base_url"].startswith("https://")
    }
    for value in extra:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("额外模型服务应为管理员确认的 HTTPS Base URL。")
        urls.add(value.rstrip("/"))
    return frozenset(urls)


@dataclass(frozen=True)
class WorkspacePolicy:
    workspace_id: str
    public_origin: str
    endpoints: frozenset[str]
    credential_cipher: Fernet
    browser_key: str
    upload_mb: int = 64
    parallel_jobs: int = 1
    cloud_sync: bool = False
    credential_key: bytes = field(default=b"", repr=False, compare=False)

    @classmethod
    def for_user(cls, principal: bytes, origin: str, endpoints: frozenset[str], master: bytes):
        workspace_id = hashlib.sha256(b"paperlens-sites-v1\0" + principal).hexdigest()

        def derive(purpose: bytes) -> bytes:
            return hmac.digest(master, purpose + b"\0" + principal, "sha256")

        return cls(
            workspace_id=workspace_id,
            public_origin=origin,
            endpoints=endpoints,
            credential_cipher=Fernet(base64.urlsafe_b64encode(derive(b"model-credentials-v1"))),
            browser_key=base64.b64encode(derive(b"browser-storage-v1")).decode("ascii"),
            credential_key=base64.urlsafe_b64encode(derive(b"model-credentials-v1")),
        )

    def seal(self, secret: str) -> str:
        if not secret:
            return ""
        return self.credential_cipher.encrypt(secret.encode()).decode("ascii")

    def unseal(self, sealed: str) -> str:
        if not sealed:
            return ""
        try:
            return self.credential_cipher.decrypt(sealed.encode("ascii")).decode()
        except (InvalidToken, UnicodeError) as exc:
            raise ValueError("无法解密这个工作区的模型凭据。请检查服务器的持久化密钥。") from exc

    def check_endpoint(self, url: str) -> None:
        if url.rstrip("/") not in self.endpoints:
            raise ValueError(
                "此模型服务地址未获服务器允许。自定义服务可由管理员添加其完整 Base URL。"
            )

    def live_html(self, template: str, navigation: bool) -> str:
        # Requests made by offlineHtml() fetch the unmodified standalone template.
        if not navigation:
            return template
        bootstrap = f'<script src="/_paperlens/client.js?workspace={self.workspace_id}"></script>'
        template = template.replace("<head>", "<head>" + bootstrap, 1)
        module = '<script type="module" crossorigin>'
        guard = (
            "if(!window.paperLensWorkspaceReady){"
            "document.body.textContent='无法确认当前研究账户，请刷新页面。';"
            "throw new Error('Workspace initialization failed');}"
        )
        if self.cloud_sync:
            guard += "await window.paperLensCloudReady;"
        return template.replace(module, module + guard, 1)
