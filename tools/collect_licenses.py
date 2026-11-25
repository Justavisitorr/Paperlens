"""Copy distribution-supplied notices and record runtime dependency provenance.

This inventories shipped metadata; it does not infer a license from a package
name or claim that code and downloaded model weights share the same license.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "docs/third_party"
UPSTREAM_NOTICES = {
    "flatbuffers": (
        "25.12.19",
        "https://raw.githubusercontent.com/google/flatbuffers/v25.12.19/LICENSE",
    ),
    "antlr4-python3-runtime": (
        "4.9.3",
        "https://raw.githubusercontent.com/antlr/antlr4/4.9.3/LICENSE.txt",
    ),
    "docling": (
        "2.129.0",
        "https://raw.githubusercontent.com/docling-project/docling/v2.129.0/LICENSE",
    ),
    "latex2mathml": (
        "3.81.1",
        "https://raw.githubusercontent.com/roniemartinez/latex2mathml/3.81.1/LICENSE",
    ),
    "rapidocr": ("3.9.2", "https://raw.githubusercontent.com/RapidAI/RapidOCR/v3.9.2/LICENSE"),
    "tokenizers": (
        "0.23.2",
        "https://raw.githubusercontent.com/huggingface/tokenizers/v0.23.2/LICENSE",
    ),
}


def copy_notice(source: Path, destination: Path) -> dict:
    raw = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)
    return {
        "path": destination.relative_to(ROOT).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def license_file(path: Path) -> bool:
    name = path.name.upper()
    if any(word in name for word in ("LICENSE", "LICENCE", "NOTICE", "COPYING")):
        return path.suffix.lower() not in {".py", ".pyc", ".pyd", ".dll"}
    return any(
        part.lower() in {"licenses", "licences"} for part in path.parts[:-1]
    ) and path.suffix.lower() in {"", ".txt", ".md", ".rst"}


def locked_packages(lock_path: Path) -> dict[str, str]:
    """Read the exact installed distributions this inventory is intended to cover."""
    return dict(
        line.split("==", 1)
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.startswith("#")
    )


def python_notices(lock_path: Path, *, fetch_missing: bool = False) -> list[dict]:
    rows = []
    for name, expected_version in locked_packages(lock_path).items():
        dist = metadata.distribution(name)
        if dist.version != expected_version:
            raise ValueError(f"{name}: installed {dist.version}, lock expects {expected_version}")
        info = dist.metadata
        row = {
            "name": info["Name"],
            "version": dist.version,
            "license_expression": info.get("License-Expression")
            or info.get("License")
            or "not declared",
            "project_urls": info.get_all("Project-URL") or [],
            "notices": [],
        }
        for entry in dist.files or []:
            if license_file(Path(str(entry))):
                source = Path(dist.locate_file(entry))
                if source.is_file():
                    suffix = hashlib.sha256(str(entry).encode()).hexdigest()[:8]
                    target = DEST / "python" / name / f"{suffix}-{source.name}"
                    row["notices"].append(copy_notice(source, target))
        if not row["notices"] and name in UPSTREAM_NOTICES:
            version, url = UPSTREAM_NOTICES[name]
            target = DEST / "python" / name / "UPSTREAM-LICENSE.txt"
            provenance = target.with_suffix(".source.json")
            if expected_version != version:
                raise ValueError(f"Update version-specific license source for {name}")
            if fetch_missing and not target.is_file():
                request = urllib.request.Request(
                    url, headers={"User-Agent": "PaperLens-license-inventory/1.0"}
                )
                with urllib.request.urlopen(request, timeout=40) as response:
                    raw = response.read(1024 * 1024)
                if b"license" not in raw.lower() or len(raw) < 100:
                    raise ValueError(f"Unexpected license response: {name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                provenance.write_text(
                    json.dumps(
                        {
                            "source_url": url,
                            "package_version": version,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            if target.is_file() and provenance.is_file():
                source_info = json.loads(provenance.read_text(encoding="utf-8"))
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
                if source_info != {"source_url": url, "package_version": version, "sha256": digest}:
                    raise ValueError(f"Stored upstream license provenance mismatch: {name}")
                row["notices"].append(
                    {
                        "path": target.relative_to(ROOT).as_posix(),
                        "sha256": digest,
                        "source_url": url,
                    }
                )
        row["notice_copyleft_mentions"] = copyleft_mentions(row["notices"])
        rows.append(row)
    return rows


def copyleft_mentions(notices: list[dict]) -> list[str]:
    """Flag notice text for review, not infer the license of an entire package."""
    patterns = {
        "AGPL": r"\bAGPL\b|Affero General Public License",
        "LGPL": r"\bLGPL\b|Lesser General Public License|Library General Public License",
        "GPL": r"\bGPL\b|(?<!Lesser )(?<!Library )(?<!Affero )GNU General Public License",
        "MPL": r"\bMPL\b|Mozilla Public License",
    }
    found: set[str] = set()
    for entry in notices:
        content = (ROOT / entry["path"]).read_text(encoding="utf-8", errors="replace")
        found.update(
            name for name, pattern in patterns.items() if re.search(pattern, content, re.I)
        )
    return sorted(found)


def model_notices() -> list[dict]:
    """Retain the installed model cards separately from software licenses."""
    models = [
        ("docling-project/docling-layout-heron", "apache-2.0"),
        ("docling-project/docling-models", "cdla-permissive-2.0"),
        ("docling-project/CodeFormulaV2", "cdla-permissive-2.0"),
    ]
    rows = []
    for repository, declared in models:
        folder = repository.replace("/", "--")
        source = ROOT / ".cache/docling/models" / folder / "README.md"
        row = {
            "repository": repository,
            "license_in_model_card": declared,
            "weights_in_submission": False,
            "model_lock": "docs/math-model-resources.lock.json"
            if repository.endswith("CodeFormulaV2")
            else "docs/model-resources.lock.json",
            "notices": [],
        }
        if source.is_file():
            text = source.read_text(encoding="utf-8")
            if not re.search(rf"^license:\s*{re.escape(declared)}\s*$", text, re.M):
                raise ValueError(f"Unexpected model card license: {repository}")
            row["notices"].append(copy_notice(source, DEST / "models" / folder / "README.md"))
        rows.append(row)
    return rows


def find_package(name: str, from_directory: Path) -> Path:
    current = from_directory
    frontend = ROOT / "frontend"
    while current.is_relative_to(frontend):
        candidate = current / "node_modules" / name / "package.json"
        if candidate.is_file():
            return candidate
        if current == frontend:
            break
        current = current.parent
    raise FileNotFoundError(f"Cannot resolve installed frontend package: {name}")


def frontend_notices() -> tuple[list[dict], str]:
    frontend = ROOT / "frontend"
    root_package = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    pending = [find_package(name, frontend) for name in root_package["dependencies"]]
    visited: set[Path] = set()
    rows, text = (
        [],
        [
            "PaperLens frontend runtime attribution\n",
            "Notices from the installed runtime dependency closure.\n"
            "Some modules may be removed by bundler tree-shaking.\n",
        ],
    )
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        name, version = data["name"], data["version"]
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        row = {
            "name": name,
            "version": version,
            "license": data.get("license", "not declared"),
            "repository": data.get("repository"),
            "notices": [],
        }
        text.append(f"\n{'=' * 72}\n{name} {version}\nDeclared license: {row['license']}\n")
        sources = list(path.parent.iterdir())
        if name == "pdfjs-dist":
            # These resources are embedded by pdfRuntime, beyond the JS package root.
            for folder in ("cmaps", "standard_fonts", "wasm"):
                sources.extend((path.parent / folder).rglob("*"))
        for source in sorted(sources):
            if source.is_file() and license_file(Path(source.name)):
                target = (
                    DEST / "frontend" / f"{safe_name}-{version}" / source.relative_to(path.parent)
                )
                row["notices"].append(copy_notice(source, target))
                text.append(
                    f"\n--- {source.relative_to(path.parent)} ---\n"
                    + source.read_text(encoding="utf-8", errors="replace")
                )
        if not row["notices"]:
            readme = path.parent / "README.md"
            if readme.is_file():
                content = readme.read_text(encoding="utf-8", errors="replace")
                if (
                    "Permission is hereby granted" in content
                    or "permission is hereby granted" in content
                ):
                    target = DEST / "frontend" / f"{safe_name}-{version}" / "README.md"
                    row["notices"].append(copy_notice(readme, target))
                    text.append("\n--- README (includes license) ---\n" + content)
        for child in data.get("dependencies", {}):
            pending.append(find_package(child, path.parent))
        row["notice_copyleft_mentions"] = copyleft_mentions(row["notices"])
        rows.append(row)
    return sorted(rows, key=lambda r: (r["name"], r["version"])), "\n".join(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch-missing",
        action="store_true",
        help="Download omitted notices from version-tagged upstream repositories",
    )
    args = parser.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)
    python = python_notices(ROOT / "requirements.txt", fetch_missing=args.fetch_missing)
    optional, ocr = [], []
    frontend, bundled = frontend_notices()
    easyread = DEST / "easyread" / "LICENSE"
    if easyread.is_file():
        bundled += "\n\nEasyRead interaction / TeX delimiter reference\n" + easyread.read_text(
            encoding="utf-8"
        )
    manifest = {
        "inventory_scope": {
            "python_runtime_lock": "requirements.txt",
            "python_docling_lock": None,
            "python_ocr_lock": None,
            "frontend_lock": "frontend/package-lock.json",
            "python_binaries_in_submission": False,
            "browser_binaries_in_submission": False,
            "model_weights_in_submission": False,
            "note": "Copied upstream notices. Copyleft mentions are review flags, not package-wide license conclusions.",
        },
        "python_runtime": python,
        "python_docling_runtime": optional,
        "python_ocr_runtime": ocr,
        "frontend_runtime_closure": frontend,
        "model_resources": [],
    }
    (DEST / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DEST / "FRONTEND_RUNTIME_NOTICES.txt").write_text(bundled, encoding="utf-8")
    asset = ROOT / "src/paperlens/assets/THIRD_PARTY_NOTICES.txt"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text(bundled, encoding="utf-8")
    missing = {
        "python": [r["name"] for r in python if not r["notices"]],
        "python_docling": [r["name"] for r in optional if not r["notices"]],
        "python_ocr": [r["name"] for r in ocr if not r["notices"]],
        "frontend": [r["name"] for r in frontend if not r["notices"]],
    }
    print(
        json.dumps(
            {
                "python": len(python),
                "python_docling": len(optional),
                "python_ocr": len(ocr),
                "frontend": len(frontend),
                "missing_notice_files": missing,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
