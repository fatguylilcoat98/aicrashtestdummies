#!/usr/bin/env python3
"""Copy the static site into dist/ for Cloudflare Pages.

The account shell is a separate process. This output has no Python.
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
FILES = (
    "index.html",
    "start.html",
    "pricing.html",
    "docs.html",
    "example.html",
    "404.html",
    "_redirects",
    "robots.txt",
    "sitemap.xml",
    "favicon.ico",
)
DIRECTORIES = ("css", "js", "assets")


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    for name in FILES:
        source = ROOT / name
        if not source.is_file():
            raise SystemExit(f"missing {name}")
        shutil.copy2(source, DIST / name)
    for folder in DIRECTORIES:
        shutil.copytree(
            ROOT / folder,
            DIST / folder,
            ignore=shutil.ignore_patterns("*.py", "__pycache__", "*.pyc"),
        )
    leaked = [str(path.relative_to(DIST)) for path in DIST.rglob("*.py")]
    if leaked:
        raise SystemExit("refusing to publish Python: " + ", ".join(leaked))


if __name__ == "__main__":
    main()
