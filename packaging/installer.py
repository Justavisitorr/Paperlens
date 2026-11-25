"""PaperLens Windows GUI installer; delegates environment setup to launch.ps1."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

from dotenv import dotenv_values

VERSION = "1.2.0"
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PAYLOAD = RESOURCES / "payload.zip"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DOWNLOAD_SOURCES = {
    "自动（官方优先，失败切换国内）": "auto",
    "国内镜像优先": "china",
    "仅官方源": "official",
}
FORBIDDEN_PARTS = {
    ".git", ".venv", ".cache", ".paperlens", "__pycache__", "node_modules",
    ".claude", ".tmp", ".test-tmp",
}


def payload_entries(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, Path]]:
    """Validate the entire source archive before writing any member."""
    entries = []
    roots = set()
    seen = set()
    for item in archive.infolist():
        normalized = item.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute() or PureWindowsPath(normalized).drive
            or ".." in path.parts or not path.parts
            or (len(path.parts) == 1 and not item.is_dir())
            or (item.external_attr >> 16) & 0o170000 == 0o120000
        ):
            raise ValueError("安装包包含不安全路径。")
        roots.add(path.parts[0])
        if len(path.parts) == 1:
            continue
        relative = Path(*path.parts[1:])
        if any(
            part.casefold() in FORBIDDEN_PARTS
            or (part.casefold().startswith(".env") and part.casefold() != ".env.example")
            or ":" in part
            for part in relative.parts
        ):
            raise ValueError("安装包包含不应分发的配置文件。")
        key = str(relative).casefold()
        if key in seen:
            raise ValueError("安装包包含重复路径。")
        seen.add(key)
        if not item.is_dir():
            entries.append((item, relative))
    if len(roots) != 1:
        raise ValueError("安装包目录结构无效。")
    required = {"pyproject.toml", "tools\\launch.ps1", ".env.example"}
    if not required.issubset({str(path).casefold() for _, path in entries}):
        # Also support running package unit tests on non-Windows hosts.
        required = {name.replace("\\", os.sep) for name in required}
        if not required.issubset({str(path).casefold() for _, path in entries}):
            raise ValueError("安装包缺少启动文件。")
    return entries


def read_payload_file(name: str) -> bytes:
    with zipfile.ZipFile(PAYLOAD) as archive:
        for item, relative in payload_entries(archive):
            if relative.as_posix() == name:
                return archive.read(item)
    raise ValueError(f"安装包缺少 {name}。")


def validate_target(target: Path) -> Path:
    target = target.expanduser().resolve()
    protected = {Path(target.anchor), Path.home().resolve()}
    for name in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(name):
            protected.add(Path(os.environ[name]).resolve())
    if target in protected:
        raise ValueError("请选择独立的 PaperLens 文件夹，不能直接安装到系统或个人根目录。")
    if target.exists() and not target.is_dir():
        raise ValueError("安装位置不是文件夹。")
    if (target / ".git").exists():
        raise ValueError("所选位置是源码开发目录。请选择单独的 PaperLens 安装文件夹。")
    if target.exists() and any(target.iterdir()):
        manifest = target / "pyproject.toml"
        try:
            project = tomllib.loads(manifest.read_text(encoding="utf-8"))["project"]["name"]
        except (OSError, ValueError, KeyError):
            project = None
        if project != "paperlens-agent" or not (target / "tools/launch.ps1").is_file():
            raise ValueError("所选文件夹已有其他文件。请选择空文件夹或现有 PaperLens 安装目录。")
        reject_running_installation(target)
    return target


def reject_running_installation(target: Path) -> None:
    """Do not replace modules beneath a service still using this installation."""
    state_file = target / ".cache/launcher/server.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8-sig"))
        if Path(state["workspace"]).resolve() != target:
            return
        port = int(state["port"])
        if not 1024 <= port <= 65535:
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    alive = False
    # PID existence is checked without signalling or terminating another process.
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, int(state.get("pid", 0)))
        if handle:
            kernel.CloseHandle(handle)
            alive = True
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            health = json.load(response)
        alive = alive or (health.get("ok") and "library" in health.get("features", []))
    except (OSError, ValueError):
        pass
    if alive:
        raise ValueError(
            "此安装目录的 PaperLens 仍在运行，不能直接升级。"
            "请先完成任务并运行该目录中的“停止 PaperLens.cmd”；关闭浏览器不会停止服务。"
            "可以重启 Windows 后先运行安装器，或选择其他空文件夹。"
        )


def checked_destination(target: Path, destination: Path) -> Path:
    if not destination.resolve().is_relative_to(target):
        raise ValueError("安装目录包含指向外部的链接，请选择其他文件夹。")
    for path in [destination, *destination.parents]:
        if path == target:
            break
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("安装目标包含文件链接，请选择普通文件夹。")
    return destination


def extract_payload(target: Path) -> int:
    target = validate_target(target)
    with zipfile.ZipFile(PAYLOAD) as archive:
        entries = payload_entries(archive)
        # Check existing reparse-point parents before modifying the installation.
        for _, relative in entries:
            destination = target / relative
            checked_destination(target, destination)
            checked_destination(target, destination.with_name(destination.name + ".installing"))
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for item, relative in entries:
            destination = target / relative
            # Example reports must never replace a user's existing reports.
            if relative.parts[0].casefold() == "outputs" and destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".installing")
            temporary.write_bytes(archive.read(item))
            temporary.replace(destination)
            copied += 1
        return copied


def parse_env(text: str) -> dict[str, str]:
    from dotenv.parser import parse_stream

    if "\x00" in text or len(text) > 256_000:
        raise ValueError(".env 内容过大或包含无效字符。")
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ValueError(f".env 第 {binding.original.line} 行语法无效。")
    return {
        key: value or ""
        for key, value in dotenv_values(stream=io.StringIO(text), interpolate=False).items()
    }


def validate_env(text: str) -> None:
    values = {key.upper(): value for key, value in parse_env(text).items()}
    schema_path = RESOURCES / "settings-schema.json"
    properties = json.loads(schema_path.read_text(encoding="utf-8")).get("properties", {})
    for name, value in values.items():
        spec = properties.get(name.lower())
        if not spec:
            continue
        kind = spec.get("type")
        try:
            if kind == "integer":
                number = int(value)
            elif kind == "number":
                number = float(value)
            elif kind == "boolean":
                if value.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                    raise ValueError
                continue
            elif kind in {"object", "array"}:
                decoded = json.loads(value)
                if not isinstance(decoded, dict if kind == "object" else list):
                    raise ValueError
                continue
            else:
                if "enum" in spec and value not in spec["enum"]:
                    raise ValueError
                continue
            if "minimum" in spec and not number >= spec["minimum"]:
                raise ValueError
            if "maximum" in spec and not number <= spec["maximum"]:
                raise ValueError
        except ValueError:
            raise ValueError(f"{name} 的值或范围不正确，请参考 .env.example。") from None
    url = urlsplit(values.get("LLM_BASE_URL", "https://api.deepseek.com"))
    if (
        url.scheme not in {"http", "https"} or not url.hostname
        or url.username or url.password or url.query or url.fragment
    ):
        raise ValueError("LLM_BASE_URL 必须为不含账户、密码或查询参数的 HTTP(S) 地址。")


def update_env(text: str, changes: dict[str, str]) -> str:
    """Keep comments and unrelated settings, replacing only explicitly edited fields."""
    for key, value in changes.items():
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError(f"{key} 不能包含换行。")
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        replacement = f"{key}='{escaped}'"
        expression = re.compile(
            rf"^(?:export\s+)?{re.escape(key)}\s*=.*$", re.MULTILINE | re.IGNORECASE
        )
        if expression.search(text):
            text = expression.sub(lambda _, replacement=replacement: replacement, text)
        else:
            text = text.rstrip() + "\n" + replacement + "\n"
    validate_env(text)
    return text


def save_env(target: Path, text: str | None) -> None:
    path = target / ".env"
    # An untouched upgrade keeps the exact existing file bytes.
    if text is None and path.exists():
        return
    text = text if text is not None else (target / ".env.example").read_text("utf-8-sig")
    validate_env(text)
    checked_destination(target, path)
    temporary = checked_destination(target, target / ".env.installing")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def saved_download_source(target: Path) -> str:
    try:
        state = json.loads((target / ".cache/launcher/network.json").read_text("utf-8-sig"))
        source = state.get("download_source")
        if source in DOWNLOAD_SOURCES.values():
            return source
    except (OSError, ValueError, AttributeError):
        pass
    return "auto"


def launch_command(
    target: Path, *, install_only: bool = True,
    download_source: str = "auto",
) -> list[str]:
    if download_source not in DOWNLOAD_SOURCES.values():
        raise ValueError("下载源选项无效。")
    command = [
        "powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(target / "tools/launch.ps1"), "-DownloadSource", download_source,
    ]
    if install_only:
        command.append("-NoLaunch")
    return command


def create_shortcut(target: Path, desktop: Path | None = None) -> None:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    launcher = target / "tools/launch.ps1"
    arguments = (
        '-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass '
        f'-File "{launcher}"'
    )
    script = "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$shell = New-Object -ComObject WScript.Shell",
        f"$desktop = {quote(str(desktop))}" if desktop else (
            "$desktop = $shell.SpecialFolders.Item('Desktop')"
        ),
        "$link = $shell.CreateShortcut((Join-Path $desktop 'PaperLens.lnk'))",
        "$link.TargetPath = (Join-Path $env:SystemRoot "
        "'System32\\WindowsPowerShell\\v1.0\\powershell.exe')",
        f"$link.Arguments = {quote(arguments)}",
        f"$link.WorkingDirectory = {quote(str(target))}",
        f"$link.IconLocation = {quote(str(target / 'packaging/PaperLens.ico'))}",
        "$link.Description = 'PaperLens'",
        "$link.WindowStyle = 7",
        "$link.Save()",
    ])
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-EncodedCommand", encoded],
        check=True, creationflags=CREATE_NO_WINDOW, capture_output=True,
    )


class Installer:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.ui_scale = self.root.winfo_fpixels("1i") / 96
        self.root.title(f"PaperLens {VERSION} 安装")
        width = min(int(740 * self.ui_scale), self.root.winfo_screenwidth() - 80)
        height = min(int(740 * self.ui_scale), self.root.winfo_screenheight() - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(
            min(int(660 * self.ui_scale), width), min(int(700 * self.ui_scale), height)
        )
        self.root.iconbitmap(str(RESOURCES / "PaperLens.ico"))
        self.events: queue.Queue = queue.Queue()
        self.process = None
        self.running = False
        self.cancelled = threading.Event()
        self.edited_env: str | None = None
        self.edited_target: Path | None = None
        default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Programs/PaperLens"
        self.target = tk.StringVar(value=str(default))
        self.download_source = tk.StringVar(value=next(iter(DOWNLOAD_SOURCES)))
        self.download_source_target: Path | None = None
        self.refresh_download_source()
        self.shortcut = tk.BooleanVar(value=True)
        self.start = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="准备安装")
        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="安装 PaperLens", font=("Microsoft YaHei UI", 21, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            frame, text=f"版本 {VERSION}  ·  Windows 10 / 11 x64\n"
            "首次安装需要联网；Python 与依赖会自动安装到所选目录。",
            padding=(0, 8, 0, 18),
        ).pack(anchor="w")
        ttk.Label(frame, text="安装位置").pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(4, 12))
        self.path_entry = ttk.Entry(row, textvariable=self.target)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.path_entry.bind("<FocusOut>", self.refresh_download_source)
        self.path_entry.bind("<Return>", self.refresh_download_source)
        self.browse_button = ttk.Button(row, text="浏览…", command=self.browse)
        self.browse_button.pack(side="left", padx=(8, 0))
        ttk.Label(frame, text="下载来源").pack(anchor="w")
        self.source_selector = ttk.Combobox(
            frame, textvariable=self.download_source,
            values=list(DOWNLOAD_SOURCES), state="readonly",
        )
        self.source_selector.pack(fill="x", pady=(4, 6))
        source_hint = ttk.Label(
            frame, text="适用于 Python、uv 和应用依赖；浏览器使用专用来源，"
            "部分模型可能转至官方地址。失败后可换源，用同一目录重试并保留缓存。",
            foreground="#666666", wraplength=int(650 * self.ui_scale),
        )
        source_hint.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(frame, variable=self.shortcut, text="创建桌面快捷方式").pack(anchor="w")
        ttk.Checkbutton(frame, variable=self.start, text="安装完成后打开 PaperLens").pack(anchor="w")
        self.advanced_button = ttk.Button(
            frame, text="高级设置…  编辑 .env", command=self.advanced,
            width=32, padding=(12, 4),
        )
        self.advanced_button.pack(anchor="w", pady=(12, 5))
        ttk.Label(
            frame, text="升级保留已有账户、文献与配置。网页中保存的设置优先于 .env。",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, textvariable=self.status).pack(anchor="w")
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=6)
        self.log = tk.Text(frame, height=8, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(14, 0), before=self.log)
        self.install_button = ttk.Button(buttons, text="开始安装", command=self.install)
        self.install_button.pack(side="right")
        self.close_button = ttk.Button(buttons, text="关闭", command=self.close)
        self.close_button.pack(side="right", padx=8)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        frame.bind("<Configure>", lambda event: source_hint.configure(
            wraplength=max(1, event.width - 48)
        ))
        self.root.after(100, self.poll)

    def refresh_download_source(self, _event=None) -> None:
        try:
            target = Path(self.target.get()).expanduser().resolve()
        except (OSError, ValueError):
            return
        if target == self.download_source_target:
            return
        self.download_source_target = target
        source = saved_download_source(target)
        self.download_source.set(next(
            label for label, value in DOWNLOAD_SOURCES.items() if value == source
        ))

    def browse(self) -> None:
        from tkinter import filedialog
        selected = filedialog.askdirectory(title="选择 PaperLens 安装文件夹", parent=self.root)
        if selected:
            self.target.set(selected)
            self.refresh_download_source()

    def env_for_target(self, target: Path) -> str:
        if self.edited_env is not None and self.edited_target == target:
            return self.edited_env
        existing = target / ".env"
        if existing.is_file():
            return existing.read_text(encoding="utf-8-sig")
        return read_payload_file(".env.example").decode("utf-8-sig")

    def advanced(self) -> None:
        from tkinter import messagebox

        target = Path(self.target.get()).expanduser().resolve()
        try:
            original = self.env_for_target(target)
            values = {key.upper(): value for key, value in parse_env(original).items()}
        except (OSError, ValueError) as exc:
            messagebox.showerror("配置无法读取", str(exc), parent=self.root)
            return
        window = self.tk.Toplevel(self.root)
        window.title("高级设置 · .env")
        window.geometry(f"{int(650 * self.ui_scale)}x{int(590 * self.ui_scale)}")
        window.transient(self.root)
        window.grab_set()
        body = self.ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        self.ttk.Label(
            body, text="配置仅在点击“开始安装”后保存到安装目录的 .env。\n"
            "访问密钥不会写入安装日志。已有网页账户设置仍然保留。",
        ).pack(anchor="w", pady=(0, 12))
        fields = {}
        for key, title in [
            ("LLM_BASE_URL", "服务地址"), ("LLM_MODEL", "模型名称"),
            ("LLM_API_KEY", "访问密钥"), ("LLM_MAX_REQUESTS", "每任务请求上限"),
        ]:
            row = self.ttk.Frame(body)
            row.pack(fill="x", pady=3)
            self.ttk.Label(row, text=title, width=16).pack(side="left")
            variable = self.tk.StringVar(value=values.get(key, ""))
            self.ttk.Entry(
                row, textvariable=variable, show="•" if key == "LLM_API_KEY" else ""
            ).pack(side="left", fill="x", expand=True)
            fields[key] = variable
        raw_enabled = self.tk.BooleanVar(value=False)
        raw = self.tk.Text(body, height=13, wrap="none", font=("Consolas", 10), undo=True)

        def toggle_raw() -> None:
            nonlocal original
            if raw_enabled.get():
                try:
                    merged = update_env(
                        original, {key: value.get() for key, value in fields.items()}
                    )
                except ValueError as exc:
                    raw_enabled.set(False)
                    messagebox.showerror("配置无效", str(exc), parent=window)
                    return
                raw.delete("1.0", "end")
                raw.insert("1.0", merged)
                raw.pack(fill="both", expand=True, pady=8)
            else:
                try:
                    candidate = raw.get("1.0", "end-1c")
                    validate_env(candidate)
                    current_values = {
                        key.upper(): value for key, value in parse_env(candidate).items()
                    }
                except ValueError as exc:
                    raw_enabled.set(True)
                    messagebox.showerror("配置无效", str(exc), parent=window)
                    return
                original = candidate
                for key, variable in fields.items():
                    variable.set(current_values.get(key, ""))
                raw.pack_forget()

        self.ttk.Checkbutton(
            body, text="编辑完整 .env（将显示密钥；保存时以此处内容为准）",
            variable=raw_enabled, command=toggle_raw,
        ).pack(anchor="w", pady=(14, 0))

        def save() -> None:
            try:
                edited = raw.get("1.0", "end-1c") if raw_enabled.get() else update_env(
                    original, {key: value.get() for key, value in fields.items()}
                )
                validate_env(edited)
            except ValueError as exc:
                messagebox.showerror("配置无效", str(exc), parent=window)
                return
            self.edited_env, self.edited_target = edited, target
            self.status.set("高级设置已准备，将在安装时写入 .env")
            window.destroy()

        self.ttk.Button(body, text="保存设置", command=save).pack(side="bottom", anchor="e", pady=10)

    def install(self) -> None:
        from tkinter import messagebox

        try:
            self.refresh_download_source()
            target = validate_target(Path(self.target.get()))
            env_text = self.edited_env if self.edited_target == target else None
            validate_env(self.env_for_target(target))
        except (ValueError, OSError) as exc:
            messagebox.showerror("不能开始安装", str(exc), parent=self.root)
            return
        self.running = True
        self.cancelled.clear()
        self.status.set("正在安装，请保持联网…")
        for widget in (
            self.install_button, self.path_entry, self.browse_button,
            self.advanced_button, self.source_selector,
        ):
            widget.configure(state="disabled")
        self.close_button.configure(text="取消安装")
        self.progress.start(15)
        threading.Thread(
            target=self.worker,
            args=(
                target, env_text, self.shortcut.get(), self.start.get(),
                DOWNLOAD_SOURCES[self.download_source.get()],
            ),
            daemon=True,
        ).start()

    def worker(
        self, target: Path, env_text: str | None, shortcut: bool,
        start: bool, download_source: str = "auto",
    ):
        try:
            copied = extract_payload(target)
            save_env(target, env_text)
            self.events.put(("log", f"已解压 {copied} 个文件。\n"))
            if self.cancelled.is_set():
                raise RuntimeError("安装已取消；重新运行可继续。")
            command = launch_command(target, download_source=download_source)
            secrets = [
                value for key, value in parse_env((target / ".env").read_text("utf-8-sig")).items()
                if ("KEY" in key or "TOKEN" in key or "PASSWORD" in key) and value
            ]
            self.process = subprocess.Popen(
                command, cwd=target, creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
            )
            for line in self.process.stdout:
                for secret in secrets:
                    line = line.replace(secret, "[已隐藏]")
                self.events.put(("log", line))
            result = self.process.wait()
            self.process = None
            if self.cancelled.is_set():
                raise RuntimeError("安装已取消；已下载内容保留，可重新安装继续。")
            if result:
                raise RuntimeError(f"安装未完成（退出码 {result}）。请检查上方日志后重试。")
            if shortcut:
                create_shortcut(target)
            if start:
                self.events.put(("log", "环境已就绪，正在启动 PaperLens…\n"))
                # A detached service can inherit a pipe's write handle after the
                # launcher exits. Wait for the launcher process, never for pipe EOF.
                startup_log = target / ".cache/launcher/gui-start.log"
                with startup_log.open("w", encoding="utf-8") as output:
                    self.process = subprocess.Popen(
                        launch_command(
                            target, install_only=False,
                            download_source=download_source,
                        ),
                        cwd=target, creationflags=CREATE_NO_WINDOW,
                        stdout=output, stderr=subprocess.STDOUT,
                    )
                    try:
                        result = self.process.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        self.process.terminate()
                        raise RuntimeError(
                            "安装已完成，但等待启动超时。请查看安装目录 .cache/launcher 中的日志。"
                        ) from None
                    finally:
                        self.process = None
                for line in startup_log.read_text("utf-8-sig", errors="replace").splitlines(True):
                    for secret in secrets:
                        line = line.replace(secret, "[已隐藏]")
                    self.events.put(("log", line))
                if self.cancelled.is_set() or result:
                    raise RuntimeError("安装已完成，但启动未完成。请查看日志或使用桌面快捷方式重试。")
            self.events.put(("done", f"安装完成：{target}"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def poll(self) -> None:
        from tkinter import messagebox

        try:
            while True:
                kind, text = self.events.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", text)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                else:
                    self.running = False
                    self.progress.stop()
                    self.status.set(text)
                    self.close_button.configure(text="关闭")
                    for widget in (
                        self.install_button, self.path_entry, self.browse_button,
                        self.advanced_button,
                    ):
                        widget.configure(state="normal")
                    self.source_selector.configure(state="readonly")
                    if kind == "error":
                        messagebox.showerror("安装未完成", text, parent=self.root)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self) -> None:
        from tkinter import messagebox

        if not self.running:
            self.root.destroy()
            return
        if messagebox.askyesno("取消安装", "停止当前安装？已下载内容将保留。", parent=self.root):
            self.cancelled.set()
            if self.process and self.process.poll() is None:
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    creationflags=CREATE_NO_WINDOW, capture_output=True, check=False,
                )


def smoke_test(result_path: Path) -> int:
    """Exercise the frozen resource loader and installation writes without downloading."""
    receipt = {"version": VERSION, "status": "failed", "checks": []}
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.iconbitmap(str(RESOURCES / "PaperLens.ico"))
        root.update()
        root.destroy()
        receipt["checks"].append("tk-and-embedded-icon")
        with tempfile.TemporaryDirectory(prefix="PaperLens-install-test-") as folder:
            target = Path(folder) / "中文 空格 安装目录"
            receipt["extracted_files"] = extract_payload(target)
            receipt["checks"].append("safe-payload-extraction-unicode-path")
            save_env(target, None)
            existing = (target / ".env").read_bytes()
            save_env(target, None)
            assert (target / ".env").read_bytes() == existing
            edited = update_env(
                existing.decode("utf-8"), {"LLM_API_KEY": "smoke-only-not-a-real-key"}
            )
            save_env(target, edited)
            assert parse_env((target / ".env").read_text("utf-8"))["LLM_API_KEY"] == (
                "smoke-only-not-a-real-key"
            )
            receipt["checks"].extend(["preserve-existing-env", "advanced-env-write"])
            create_shortcut(target, Path(folder))
            assert (Path(folder) / "PaperLens.lnk").is_file()
            receipt["checks"].append("windows-shortcut")
            config = tomllib.loads((target / "pyproject.toml").read_text("utf-8"))
            assert config["project"]["version"] == VERSION
            receipt["checks"].append("embedded-version")
        receipt["status"] = "passed"
        receipt["network_install_tested"] = False
    except Exception as exc:
        receipt["error"] = str(exc)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if receipt["status"] == "passed" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-test", type=Path, metavar="RESULT_JSON")
    args = parser.parse_args()
    if args.smoke_test:
        return smoke_test(args.smoke_test.resolve())
    Installer().root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
