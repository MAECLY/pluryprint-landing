"""What the tests share: the repository's paths, the release fixture, and a
way to run or load the scripts.

The landing follows the published release on its own:

- nothing in site/ names a version, a release date, a download link or a file
  size; those places carry {{release.*}} markers;
- scripts/apply_release.py fills them from the GitHub API record of the
  latest release into a copy of site/, and refuses a release that lacks any of
  the four installers or SHA256SUMS;
- scripts/build.py, the Vercel build command, fetches that record and runs
  apply_release.py into dist/, which is what Vercel serves, never site/.

Standard library only: `python3 -I -m unittest discover -s tests -v`.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "release.json"

BASE = "https://github.com/MAECLY/pluryprint/releases"

LABELS = [
    ("windows_x64", "exe"),
    ("windows_arm64", "exe"),
    ("apple_silicon", "dmg"),
    ("apple_intel", "dmg"),
]

TEXT_SUFFIXES = {".html", ".txt", ".webmanifest", ".xml", ".json"}


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def site_texts(root: Path) -> list[tuple[str, str]]:
    """Every text file under root, as (path relative to root, text)."""
    out = [
        (p.relative_to(root).as_posix(), p.read_text(encoding="utf-8"))
        for p in root.rglob("*")
        if p.is_file() and p.suffix in TEXT_SUFFIXES
    ]
    out.sort()
    assert len(out) >= 6, out
    return out


def fixture() -> dict:
    """A release 9.8.7 published 2027-03-05 whose four installers weigh
    10.0, 11.0, 12.0 and 13.0 MiB plus a little, with SHA256SUMS and the
    updater's files beside them. A fresh copy each time, to mutate freely."""
    return copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))


def run_apply_release(release_json: Path, site: Path, out: Path):
    """Runs apply_release.py as the build would, with this interpreter."""
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPTS / "apply_release.py"),
            "--release-json",
            str(release_json),
            "--repo",
            "MAECLY/pluryprint",
            "--site",
            str(site),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )


def load_script(name: str):
    """scripts/<name>.py as a fresh module, for tests that patch it."""
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"landing_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
