"""Package prefetch delegates resolution and installation to uv."""

import copy
import datetime
import hashlib
import importlib.util
import tomllib
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.setup_packages", Path(__file__).resolve().parents[1] / "tools/setup_packages.py"
)
packages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packages)


def wheel_lock(url="https://example.com/demo-1.0-py3-none-any.whl"):
    return {
        "lock-version": "1.0", "created-by": "uv", "requires-python": ">=3.12",
        "packages": [{
            "name": "demo", "version": "1.0", "marker": "sys_platform == 'win32'",
            "wheels": [{"url": url, "size": 3, "hashes": {
                "sha256": hashlib.sha256(b"abc").hexdigest(),
            }}],
            "sdist": {"url": "https://example.com/unused.tar.gz"},
        }],
    }


def test_prefetch_selects_only_wheel_and_preserves_metadata(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(packages, "download_resource", lambda *args: calls.append(args))
    lock = packages.prefetch_lock(wheel_lock(), tmp_path)
    package = lock["packages"][0]
    assert len(calls) == 1
    assert package["marker"] == "sys_platform == 'win32'"
    assert "sdist" not in package
    wheel = package["wheels"][0]
    assert "url" not in wheel
    assert Path(wheel["path"]).parent.name == hashlib.sha256(b"abc").hexdigest()
    assert calls[0][2:] == (3, hashlib.sha256(b"abc").hexdigest())


def test_mirror_switch_uses_same_artifact_target(tmp_path, monkeypatch):
    targets = []
    monkeypatch.setattr(packages, "download_resource", lambda url, target, *args: targets.append(target))
    for host in ("official.example", "mirror.example"):
        packages.prefetch_lock(wheel_lock(f"https://{host}/demo-1.0-py3-none-any.whl"), tmp_path)
    assert targets[0] == targets[1]


def test_source_only_and_editable_entries(tmp_path, monkeypatch):
    source = wheel_lock()["packages"][0]
    source["sdist"] = source.pop("wheels")[0]
    source["sdist"]["url"] = "https://example.com/demo-1.0.tar.gz"
    directory = {"name": "app", "directory": {"path": "../..", "editable": True}}
    original = copy.deepcopy(directory)
    calls = []
    monkeypatch.setattr(packages, "download_resource", lambda *args: calls.append(args))
    lock = packages.prefetch_lock({"packages": [source, directory]}, tmp_path)
    assert len(calls) == 1
    assert lock["packages"][0]["sdist"]["path"].endswith("demo-1.0.tar.gz")
    assert lock["packages"][1] == original


@pytest.mark.parametrize("change", ["missing-sha", "invalid-size", "unsafe-name"])
def test_ambiguous_or_unverifiable_artifact_fails_before_download(tmp_path, monkeypatch, change):
    lock = wheel_lock()
    wheel = lock["packages"][0]["wheels"][0]
    if change == "missing-sha":
        wheel["hashes"] = {}
    elif change == "invalid-size":
        wheel["size"] = -1
    else:
        wheel["url"] = "https://example.com/..%2Foutside.whl"
    calls = []
    monkeypatch.setattr(packages, "download_resource", lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        packages.prefetch_lock(lock, tmp_path)
    assert not calls


def test_multiple_wheels_use_standard_target_tag_priority(tmp_path, monkeypatch):
    lock = wheel_lock()
    native = copy.deepcopy(lock["packages"][0]["wheels"][0])
    native["url"] = "https://example.com/demo-1.0-cp312-cp312-win_amd64.whl"
    lock["packages"][0]["wheels"].append(native)
    calls = []
    monkeypatch.setattr(packages, "download_resource", lambda *args: calls.append(args))
    packages.prefetch_lock(lock, tmp_path, ["cp312-cp312-win_amd64", "py3-none-any"])
    assert calls[0][0] == "https://example.com/demo-1.0-cp312-cp312-win_amd64.whl"
    assert len(lock["packages"][0]["wheels"]) == 1


def test_incompatible_wheels_are_rejected():
    with pytest.raises(ValueError, match="No wheel matches"):
        packages.select_wheel(wheel_lock()["packages"][0]["wheels"], ["cp312-cp312-win_amd64"])


def test_size_probe_uses_head_without_reading_payload(monkeypatch):
    class Response:
        headers = {"Content-Length": "123"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    calls = []
    monkeypatch.setattr(packages, "urlopen", lambda request, **kwargs: calls.append((request, kwargs)) or Response())
    assert packages.artifact_size("https://example.com/large.whl") == 123
    request, options = calls[0]
    assert request.get_method() == "HEAD"
    assert options == {"timeout": 30}


def test_missing_size_uses_headers_and_keeps_hash_verification(tmp_path, monkeypatch):
    lock = wheel_lock()
    lock["packages"][0]["wheels"][0].pop("size")
    probes = []
    calls = []
    monkeypatch.setattr(packages, "artifact_size", lambda url: probes.append(url) or 3)
    monkeypatch.setattr(packages, "download_resource", lambda *args: calls.append(args))
    packages.prefetch_lock(lock, tmp_path)
    assert len(probes) == 1
    assert calls[0][2:] == (3, hashlib.sha256(b"abc").hexdigest())
    assert lock["packages"][0]["wheels"][0]["size"] == 3


def test_lock_roundtrips_unknown_nested_metadata_and_timestamps(tmp_path):
    lock = wheel_lock()
    lock["extras"] = ["docling"]
    lock["packages"][0]["wheels"][0]["upload-time"] = datetime.datetime(
        2026, 1, 2, tzinfo=datetime.UTC
    )
    lock["tool"] = {"future": {"enabled": True, "note": "中文\\路径\n新行", "value": 1.5}}
    path = tmp_path / "pylock.local.toml"
    packages.write_lock(path, lock)
    assert tomllib.loads(path.read_text("utf-8")) == lock


def test_install_uses_uv_for_resolution_and_preserves_optional_packages(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-e .\n--extra-index-url https://torch.example/cpu\n")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "compile" in command:
            packages.write_lock(Path(command[command.index("--output-file") + 1]), wheel_lock())

    monkeypatch.setattr(packages.subprocess, "run", run)
    monkeypatch.setattr(packages, "target_wheel_tags", lambda python: ["py3-none-any"])
    monkeypatch.setattr(packages, "download_resource", lambda *args: None)
    packages.install_packages(
        tmp_path / "uv.exe", tmp_path / "python.exe", requirements, tmp_path,
        "https://mirror.example/simple", ["https://torch.example/cpu"],
    )
    assert len(calls) == 2
    compile_command, install_command = [call[0] for call in calls]
    assert "compile" in compile_command
    assert compile_command[compile_command.index("--python-platform") + 1] == "windows"
    assert "install" in install_command and "sync" not in install_command
    for command, kwargs in calls:
        assert command[command.index("--default-index") + 1] == "https://mirror.example/simple"
        assert command[command.index("--index") + 1] == "https://torch.example/cpu"
        assert command[command.index("--index-strategy") + 1] == "unsafe-best-match"
        assert kwargs == {"cwd": tmp_path.resolve(), "check": True}
    local = tomllib.loads((tmp_path / ".cache/launcher/pylock.packages-local.toml").read_text("utf-8"))
    assert "path" in local["packages"][0]["wheels"][0]
