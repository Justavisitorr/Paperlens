"""Build the single-file Windows GUI installer from a verified source archive.

Run with the project's Python. PyInstaller is installed only in the dedicated
.cache/windows-build/venv; application ML dependencies are never frozen.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".cache/windows-build"


def run(command: list[str]) -> None:
    environment = dict(os.environ)
    # Avoid collecting unrelated native DLLs from the author's development tools.
    environment["PATH"] = os.pathsep.join([
        str(Path(command[0]).parent),
        str(Path(sys.base_prefix)),
        str(Path(os.environ["SystemRoot"]) / "System32"),
    ])
    subprocess.run(command, cwd=ROOT, check=True, env=environment)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Windows x64 is required to build the Windows installer.")
    sys.path.insert(0, str(ROOT / "packaging"))
    from installer import payload_entries

    from paperlens.config import Settings

    with zipfile.ZipFile(args.payload) as archive:
        payload_entries(archive)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    environment = BUILD / "venv"
    python = environment / "Scripts/python.exe"
    if not python.is_file():
        run([sys.executable, "-m", "venv", str(environment)])
    run([
        str(python), "-m", "pip", "install", "--disable-pip-version-check",
        "pyinstaller==6.22.0", "python-dotenv==1.2.3",
    ])
    staging = BUILD / "resources"
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.payload, staging / "payload.zip")
    (staging / "settings-schema.json").write_text(
        json.dumps(Settings.model_json_schema(), ensure_ascii=False), encoding="utf-8"
    )
    if not (ROOT / "packaging/PaperLens.ico").is_file():
        parser.error("packaging/PaperLens.ico must be present before building.")
    # Preserve the licenses of the runtime and libraries embedded in the installer.
    notices = [
        "PaperLens installer third-party notices\n",
        "Python: https://docs.python.org/3/license.html\n",
        "Tcl/Tk: https://www.tcl-lang.org/software/tcltk/license.html\n",
        "PyInstaller: https://pyinstaller.org/en/stable/license.html\n",
        "python-dotenv: https://github.com/theskumar/python-dotenv/blob/main/LICENSE\n\n",
    ]
    license_paths = [Path(sys.base_prefix) / "LICENSE.txt"]
    license_paths += sorted((ROOT / "packaging/licenses").glob("*"))
    license_paths += list((Path(sys.base_prefix) / "tcl").glob("*/license.terms"))
    site_packages = environment / "Lib/site-packages"
    for pattern in ("pyinstaller-*.dist-info/licenses/*", "python_dotenv-*.dist-info/licenses/*"):
        license_paths += list(site_packages.glob(pattern))
    for path in license_paths:
        if path.is_file():
            notices.extend([f"\n--- {path.name} ---\n", path.read_text("utf-8", errors="replace")])
    notice_text = "".join(notices)
    if len(license_paths) < 4:
        raise RuntimeError("Bundled runtime license files are incomplete.")
    (staging / "INSTALLER_THIRD_PARTY_NOTICES.txt").write_text(notice_text, encoding="utf-8")
    (ROOT / "packaging/INSTALLER_THIRD_PARTY_NOTICES.txt").write_text(
        notice_text, encoding="utf-8"
    )
    version_file = BUILD / "version.txt"
    parts = tuple(int(part) for part in version.split(".")) + (0,)
    version_file.write_text(
        "VSVersionInfo(ffi=FixedFileInfo("
        f"filevers={parts!r}, prodvers={parts!r}, mask=0x3f, flags=0x0, "
        "OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)), "
        "kids=[StringFileInfo([StringTable('040904B0', ["
        "StringStruct('CompanyName', 'PaperLens'),"
        "StringStruct('FileDescription', 'PaperLens GUI Installer'),"
        f"StringStruct('FileVersion', '{version}'),"
        "StringStruct('ProductName', 'PaperLens'),"
        f"StringStruct('ProductVersion', '{version}'),"
        f"StringStruct('OriginalFilename', 'PaperLens-{version}-Setup.exe')"
        "])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])",
        encoding="utf-8",
    )
    run([
        str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--noupx", "--name", f"PaperLens-{version}-Setup",
        "--icon", str(ROOT / "packaging/PaperLens.ico"),
        "--version-file", str(version_file),
        "--distpath", str(args.output.resolve()),
        "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
        "--add-data", f"{staging};.",
        "--add-data", f"{ROOT / 'packaging/PaperLens.ico'};.",
        str(ROOT / "packaging/installer.py"),
    ])
    print(json.dumps({
        "installer": str(args.output.resolve() / f"PaperLens-{version}-Setup.exe"),
        "version": version, "payload": str(args.payload.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
