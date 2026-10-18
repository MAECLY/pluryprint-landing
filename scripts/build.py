#!/usr/bin/env python3
"""Build the landing into dist/ from site/ and the app's latest release.

This is the Vercel build command (vercel.json runs `python3 -I
scripts/build.py` and serves dist/). It fetches the GitHub API record of the
latest published release of MAECLY/pluryprint, empties dist/ and runs
apply_release.py, which copies site/ there with every {{release.*}} marker
filled and refuses a release the landing cannot link.

Every failure (the API unreachable or answering with an error, a release
without one of its installers, a marker left over) exits non-zero with a
message on stderr. Vercel then fails the build and the live site keeps its
previous deployment, so the landing never links a file that is not there.

  build.py [--release-json FILE] [--out DIR]

--release-json reads the release from a file instead of the API, for offline
builds and the tests. --out builds somewhere other than dist/; only dist/ is
emptied first, any other folder must be empty or not exist yet.

GITHUB_TOKEN or GH_TOKEN, when set, is sent as a bearer token so the build is
not held to the API's 60 requests an hour per address. It is never printed.
The release of a public repository needs no token at all.

Plain standard-library Python (3.9 or later), so the build needs nothing
installed. Paths are relative to the repository, not the current folder.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
DIST = ROOT / "dist"

REPO = "MAECLY/pluryprint"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
API_VERSION = "2022-11-28"
USER_AGENT = "pluryprint-landing-build"
TIMEOUT = 30  # seconds per request

# Pauses before the second and third attempts. Only network errors and 5xx
# answers are retried: a 4xx will not change by asking again.
RETRY_DELAYS = (2, 5)


class BuildError(Exception):
    pass


def load_apply_release():
    """apply_release.py as a module.

    `python3 -I` keeps the script's own folder off sys.path, so it is loaded
    from its path rather than imported by name.
    """
    path = Path(__file__).resolve().parent / "apply_release.py"
    spec = importlib.util.spec_from_file_location("apply_release", path)
    if spec is None or spec.loader is None:
        raise BuildError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def fetch_release() -> bytes:
    """The latest release's JSON, as the API sends it."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    secret = token()
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(API, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            # The reason GitHub gives, never the request (it holds the token).
            why = f"GET {API} answered {e.code} {e.reason}"
            if e.code < 500 or attempt == attempts:
                if e.code == 404:
                    why += " (no published release)"
                elif e.code in (401, 403, 429):
                    why += " (rate limited or a bad token)"
                raise BuildError(why) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            why = f"GET {API} failed: {reason}"
            if attempt == attempts:
                raise BuildError(why) from None
        delay = RETRY_DELAYS[attempt - 1]
        print(f"build: {why}; retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
    raise BuildError("unreachable")  # the loop always returns or raises


def build(release_json: Path | None, out: Path) -> int:
    apply_release = load_apply_release()
    with tempfile.TemporaryDirectory(prefix="pluryprint-landing-") as tmp:
        if release_json is None:
            body = fetch_release()
            try:
                json.loads(body)
            except ValueError as e:
                raise BuildError(f"GET {API} sent no JSON: {e}") from None
            release_json = Path(tmp) / "release.json"
            release_json.write_bytes(body)

        # dist/ only ever holds a build, so an old one is removed; any other
        # folder is left to apply_release.py, which refuses a non-empty one.
        if out.resolve() == DIST.resolve() and out.exists():
            shutil.rmtree(out)

        code = apply_release.main([
            "--release-json", str(release_json),
            "--repo", REPO,
            "--site", str(SITE),
            "--out", str(out),
        ])
    if code != 0:
        # Half a build is no build: do not leave it where a preview would
        # serve it.
        if out.resolve() == DIST.resolve() and out.exists():
            shutil.rmtree(out)
        raise BuildError(f"apply_release.py refused the release (exit {code})")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument(
        "--release-json",
        type=Path,
        help="read the release from this file instead of the GitHub API",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DIST,
        help="where to build (default: dist/ in the repository)",
    )
    a = p.parse_args(argv)
    try:
        return build(a.release_json, a.out)
    except (OSError, BuildError) as e:
        print(f"build: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
