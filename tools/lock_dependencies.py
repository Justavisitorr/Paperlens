"""Record the installed transitive runtime set for this tested Python/platform.

Run after dependency resolution and before the clean-install check. The runtime uses AI reading without local model weights.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import platform
import sys
import tomllib
from collections import deque
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pending = deque(Requirement(r) for r in project["project"]["dependencies"])
    visited: set[tuple[str, tuple[str, ...]]] = set()
    versions = {}
    while pending:
        req = pending.popleft()
        name = canonicalize_name(req.name)
        key = (name, tuple(sorted(req.extras)))
        if key in visited:
            continue
        visited.add(key)
        dist = metadata.distribution(name)
        if req.specifier and dist.version not in req.specifier:
            raise RuntimeError(f"Installed {name} does not satisfy {req.specifier}")
        versions[name] = dist.version
        for raw in dist.requires or []:
            child = Requirement(raw)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in {"", *req.extras}
            ):
                pending.append(child)
    header = [
        "# Runtime dependency lock generated from the tested environment.",
        f"# Python {sys.version.split()[0]} / {platform.system()} {platform.machine()}",
        "# Install from project root: python -m pip install -r requirements.txt",
        "# AI reading runtime; no local OCR, Docling or PyTorch installation.",
        "-e .",
        "",
    ]
    lines = [f"{name}=={version}" for name, version in sorted(versions.items())]
    filename = "requirements.txt"
    (root / filename).write_text("\n".join(header + lines) + "\n", encoding="utf-8")
    print(f"Locked {len(versions)} runtime distributions.")


if __name__ == "__main__":
    main()
