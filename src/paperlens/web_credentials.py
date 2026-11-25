"""Service access stays in the OS credential store or a mounted runtime secret."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def windows_access(site: str, new: str | None = None) -> str:
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    target = "PaperLens/Sites/" + urlsplit(site).netloc
    if new is not None:
        blob = (ctypes.c_ubyte * len(new.encode())).from_buffer_copy(new.encode())
        entry = _Credential(
            Type=1,
            TargetName=target,
            CredentialBlobSize=len(blob),
            CredentialBlob=blob,
            Persist=2,
            UserName="PaperLens Agent",
        )
        api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        if not api.CredWriteW(ctypes.byref(entry), 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return ""
    result = ctypes.POINTER(_Credential)()
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_Credential)),
    ]
    if not api.CredReadW(target, 1, 0, ctypes.byref(result)):
        raise RuntimeError("当前 Windows 账户尚未保存此站点的服务访问凭据。")
    try:
        return ctypes.string_at(
            result.contents.CredentialBlob, result.contents.CredentialBlobSize
        ).decode()
    finally:
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree(result)


def service_access(site: str) -> str:
    filename = os.getenv("PAPERLENS_SITE_ACCESS_FILE")
    if filename:
        return Path(filename).read_text("utf-8").strip()
    if os.name == "nt":
        return windows_access(site)
    raise RuntimeError("请用 PAPERLENS_SITE_ACCESS_FILE 指定已挂载的站点服务访问凭据。")
