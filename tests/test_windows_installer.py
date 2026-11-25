"""Installer tests focus on credentials, upgrades and archive write boundaries."""

import importlib.util
import json
import queue
import threading
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "paperlens_installer", Path(__file__).resolve().parents[1] / "packaging/installer.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def resources(tmp_path, monkeypatch):
    from paperlens.config import Settings

    (tmp_path / "settings-schema.json").write_text(
        json.dumps(Settings.model_json_schema()), encoding="utf-8"
    )
    monkeypatch.setattr(installer, "RESOURCES", tmp_path)
    return tmp_path


def test_env_preserves_existing_and_roundtrips_special_characters(resources):
    path = resources / ".env"
    path.write_text("# original\nLLM_MODEL='existing'\n", encoding="utf-8")
    original = path.read_bytes()
    installer.save_env(resources, None)
    assert path.read_bytes() == original
    secret = "test'quote\\slash$literal#hash"
    edited = installer.update_env(path.read_text("utf-8"), {"LLM_API_KEY": secret})
    installer.save_env(resources, edited)
    assert installer.parse_env(path.read_text("utf-8"))["LLM_API_KEY"] == secret
    assert "# original" in edited and "LLM_MODEL='existing'" in edited


@pytest.mark.parametrize("text", [
    "LLM_MAX_REQUESTS=-1", "LLM_BASE_URL=file:///secrets",
    "LLM_BASE_URL=https://user:password@example.com",
    "PAPERLENS_MAX_PAGES=no", "PAPERLENS_MATH_ENABLED=maybe", "LLM_MODEL='unclosed",
    "LLM_ROUTES=not-json", "llm_base_url=file:///invalid",
    "LLM_ROUTES=[]",
])
def test_invalid_env_is_rejected_without_leaking_value(resources, text):
    with pytest.raises(ValueError) as error:
        installer.validate_env(text)
    assert "password" not in str(error.value)


@pytest.mark.parametrize("bad_name", [
    "root/../../outside.txt", "C:/outside.txt", "root/.env",
    "root/.paperlens/connections.json", "root/file:stream", "root/.venv/python.exe",
    "root/.ENV", "root/.PaperLens/connections.json",
])
def test_unsafe_payload_rejected_before_writing(resources, monkeypatch, bad_name):
    payload = resources / "bad.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("root/", b"")
        archive.writestr("root/pyproject.toml", '[project]\nname="paperlens-agent"')
        archive.writestr("root/tools/launch.ps1", "exit 0")
        archive.writestr("root/.env.example", "")
        archive.writestr(bad_name, "bad")
    monkeypatch.setattr(installer, "PAYLOAD", payload)
    target = resources / "new"
    with pytest.raises(ValueError):
        installer.extract_payload(target)
    assert not target.exists()


def test_refuses_unrelated_existing_directory(resources):
    (resources / "personal.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        installer.validate_target(resources)
    assert (resources / "personal.txt").read_text() == "keep"


def test_refuses_source_checkout(resources):
    (resources / ".git").mkdir()
    with pytest.raises(ValueError, match="源码"):
        installer.validate_target(resources)


def test_refuses_registered_running_service(resources, monkeypatch):
    (resources / ".cache/launcher").mkdir(parents=True)
    (resources / ".cache/launcher/server.json").write_text(json.dumps({
        "workspace": str(resources), "port": 8000, "pid": 0,
    }), encoding="utf-8")

    class Response:
        def __enter__(self):
            import io
            return io.StringIO('{"ok": true, "features": ["library"]}')

        def __exit__(self, *args):
            return False

    class Opener:
        def open(self, url, timeout):
            return Response()

    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="仍在运行"):
        installer.reject_running_installation(resources)


def test_upgrade_preserves_existing_examples_and_env(resources, monkeypatch):
    payload = resources / "safe.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("root/", b"")
        archive.writestr("root/pyproject.toml", '[project]\nname="paperlens-agent"')
        archive.writestr("root/tools/launch.ps1", "exit 0")
        archive.writestr("root/.env.example", "")
        archive.writestr("root/outputs/bert/report.json", "sample")
    monkeypatch.setattr(installer, "PAYLOAD", payload)
    target = resources / "app"
    installer.extract_payload(target)
    (target / ".env").write_text("LLM_MODEL=mine")
    (target / "outputs/bert/report.json").write_text("user-report")
    installer.extract_payload(target)
    assert (target / "outputs/bert/report.json").read_text() == "user-report"
    assert (target / ".env").read_text() == "LLM_MODEL=mine"


@pytest.mark.parametrize("source", ["auto", "china", "official"])
@pytest.mark.parametrize("install_only", [True, False])
def test_launch_command_passes_download_source(tmp_path, source, install_only):
    command = installer.launch_command(
        tmp_path / "中文 空格", install_only=install_only,
        download_source=source,
    )
    assert command[command.index("-DownloadSource") + 1] == source
    assert "-Full" not in command
    assert ("-NoLaunch" in command) == install_only


def test_launch_command_rejects_unknown_source(tmp_path):
    with pytest.raises(ValueError, match="下载源"):
        installer.launch_command(tmp_path, download_source="unknown")


@pytest.mark.parametrize("content, expected", [
    ('{"download_source":"china"}', "china"),
    ('{"download_source":"official"}', "official"),
    ('{"download_source":"unknown"}', "auto"),
    ('{"download_source":[]}', "auto"),
    ('[]', "auto"), ('null', "auto"), ('broken', "auto"),
])
def test_load_saved_download_source(tmp_path, content, expected):
    network = tmp_path / ".cache/launcher/network.json"
    network.parent.mkdir(parents=True)
    network.write_text(content, encoding="utf-8-sig")
    assert installer.saved_download_source(tmp_path) == expected


def test_missing_download_preference_defaults_to_auto(tmp_path):
    assert installer.saved_download_source(tmp_path) == "auto"


def test_target_preference_load_preserves_current_choice(tmp_path):
    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    network = tmp_path / ".cache/launcher/network.json"
    network.parent.mkdir(parents=True)
    network.write_text('{"download_source":"china"}', encoding="utf-8")
    gui = object.__new__(installer.Installer)
    gui.target = Variable(str(tmp_path))
    gui.download_source = Variable("")
    gui.download_source_target = None
    gui.refresh_download_source()
    assert installer.DOWNLOAD_SOURCES[gui.download_source.get()] == "china"
    gui.download_source.set("仅官方源")
    gui.refresh_download_source()
    assert gui.download_source.get() == "仅官方源"
    gui.target.set(str(tmp_path / "other"))
    gui.refresh_download_source()
    assert installer.DOWNLOAD_SOURCES[gui.download_source.get()] == "auto"
    gui.target.set(str(tmp_path))
    gui.refresh_download_source()
    assert installer.DOWNLOAD_SOURCES[gui.download_source.get()] == "china"


def test_worker_uses_selected_source_for_install_and_start(resources, monkeypatch):
    (resources / ".env").write_text("", encoding="utf-8")
    (resources / ".cache/launcher").mkdir(parents=True)
    monkeypatch.setattr(installer, "extract_payload", lambda target: 3)
    commands = []

    class Process:
        stdout = []

        def wait(self, **kwargs):
            return 0

    def popen(command, **kwargs):
        commands.append(command)
        return Process()

    monkeypatch.setattr(installer.subprocess, "Popen", popen)
    gui = object.__new__(installer.Installer)
    gui.events = queue.Queue()
    gui.cancelled = threading.Event()
    gui.worker(resources, None, False, True, "china")
    assert len(commands) == 2
    assert all(command[command.index("-DownloadSource") + 1] == "china" for command in commands)
    assert "-NoLaunch" in commands[0] and "-NoLaunch" not in commands[1]
    assert list(gui.events.queue)[-1][0] == "done"


def test_failed_install_restores_source_selector_for_retry(monkeypatch):
    from tkinter import messagebox

    class Widget:
        def __init__(self):
            self.options = {}

        def configure(self, **options):
            self.options.update(options)

    class Progress:
        def stop(self):
            pass

    class Root:
        def after(self, *args):
            pass

    class Status:
        def set(self, text):
            self.text = text

    monkeypatch.setattr(messagebox, "showerror", lambda *args, **kwargs: None)
    gui = object.__new__(installer.Installer)
    gui.root = Root()
    gui.events = queue.Queue()
    gui.events.put(("error", "download interrupted"))
    gui.running = True
    gui.progress = Progress()
    gui.status = Status()
    for name in (
        "close_button", "install_button", "path_entry", "browse_button",
        "advanced_button", "source_selector",
    ):
        setattr(gui, name, Widget())
    gui.poll()
    assert not gui.running
    assert gui.install_button.options["state"] == "normal"
    assert gui.source_selector.options["state"] == "readonly"
    assert gui.status.text == "download interrupted"
