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

It also asks the API for two public metrics, the downloads of every asset of
every release (GET /repos/MAECLY/pluryprint/releases, every page) and the
repository's stars (GET /repos/MAECLY/pluryprint), which the page shows only
when known and above zero. Unlike the release, a metric the API does not give
fails nothing: the build says so on stderr and the page shows no number for
it, never a guessed or a zero one.

  build.py [--release-json FILE] [--metrics-json FILE] [--out DIR]

--release-json reads the release from a file instead of the API, for offline
builds and the tests; such a build asks the API for nothing, so it shows no
metric unless --metrics-json gives them ({"downloads": N, "stars": N}). --out builds somewhere other than dist/; only dist/ is
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
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
REPO_API = f"https://api.github.com/repos/{REPO}"
PER_PAGE = 100
MAX_PAGES = 50  # 5000 releases: far more than there will ever be
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


def fetch(url: str) -> bytes:
    """A GET to the API, as it answers; BuildError if it does not."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    secret = token()

    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=headers, method="GET")
        if secret:
            # Unredirected: urllib copies ordinary headers to wherever a
            # redirect points, and the token is only for api.github.com.
            request.add_unredirected_header("Authorization", f"Bearer {secret}")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            # The reason GitHub gives, never the request (it holds the token).
            why = f"GET {url} answered {e.code} {e.reason}"
            if e.code < 500 or attempt == attempts:
                if e.code == 404:
                    why += " (no published release)"
                elif e.code in (401, 403, 429):
                    why += " (rate limited or a bad token)"
                raise BuildError(why) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            why = f"GET {url} failed: {reason}"
            if attempt == attempts:
                raise BuildError(why) from None
        delay = RETRY_DELAYS[attempt - 1]
        print(f"build: {why}; retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
    raise BuildError("unreachable")  # the loop always returns or raises


def fetch_release() -> bytes:
    """The latest release's JSON, as the API sends it."""
    return fetch(API)


def fetch_json(url: str):
    body = fetch(url)
    try:
        return json.loads(body)
    except ValueError as e:
        raise BuildError(f"GET {url} sent no JSON: {e}") from None


def count(value, what: str) -> int:
    # bool is an int in Python; the API never sends one for a count.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BuildError(f"{what} is {value!r}, not a count")
    return value


def fetch_downloads() -> int:
    """download_count summed over every asset of every release, all pages."""
    total = 0
    for page in range(1, MAX_PAGES + 1):
        url = f"{RELEASES_API}?per_page={PER_PAGE}&page={page}"
        releases = fetch_json(url)
        if not isinstance(releases, list):
            raise BuildError(f"GET {url} sent no list of releases")
        for release in releases:
            if not isinstance(release, dict):
                raise BuildError(f"GET {url} sent a release that is no object")
            assets = release.get("assets")
            if not isinstance(assets, list):
                raise BuildError(f"GET {url} sent a release without assets")
            for asset in assets:
                if not isinstance(asset, dict):
                    raise BuildError(f"GET {url} sent an asset that is no object")
                total += count(asset.get("download_count"), "a download_count")
        if len(releases) < PER_PAGE:
            return total
    raise BuildError(f"GET {RELEASES_API} has more than {MAX_PAGES} pages")


def fetch_stars() -> int:
    repo = fetch_json(REPO_API)
    if not isinstance(repo, dict):
        raise BuildError(f"GET {REPO_API} sent no JSON object")
    return count(repo.get("stargazers_count"), "stargazers_count")


def fetch_metrics() -> dict:
    """{"downloads": N or None, "stars": N or None}: None when unknown.

    A metric is decoration, not the release: an API that fails or limits the
    rate leaves it out of the page instead of failing the build.
    """
    metrics = {}
    for name, get in [("downloads", fetch_downloads), ("stars", fetch_stars)]:
        try:
            metrics[name] = get()
        except BuildError as e:
            metrics[name] = None
            print(f"build: {name} unknown, the page shows none: {e}", file=sys.stderr)
    return metrics


def build(
    release_json: Path | None, out: Path, metrics_json: Path | None = None
) -> int:
    apply_release = load_apply_release()
    with tempfile.TemporaryDirectory(prefix="pluryprint-landing-") as tmp:
        fetched = release_json is None
        if fetched:
            body = fetch_release()
            try:
                parsed = json.loads(body)
            except ValueError as e:
                raise BuildError(f"GET {API} sent no JSON: {e}") from None
            if not isinstance(parsed, dict):
                raise BuildError(f"GET {API} sent no JSON object")
            release_json = Path(tmp) / "release.json"
            release_json.write_bytes(body)
        if metrics_json is None and fetched:
            metrics_json = Path(tmp) / "metrics.json"
            metrics_json.write_text(json.dumps(fetch_metrics()), encoding="utf-8")

        # dist/ only ever holds a build, so an old one is removed; any other
        # folder is left to apply_release.py, which refuses a non-empty one.
        if out.resolve() == DIST.resolve() and out.exists():
            shutil.rmtree(out)

        args = [
            "--release-json", str(release_json),
            "--repo", REPO,
            "--site", str(SITE),
            "--out", str(out),
        ]
        if metrics_json is not None:
            args += ["--metrics-json", str(metrics_json)]
        code = None  # stays None if apply_release.py crashes
        try:
            code = apply_release.main(args)
        finally:
            # Half a build is no build: do not leave it where a preview would
            # serve it, whether the script refused or crashed.
            if code != 0 and out.resolve() == DIST.resolve() and out.exists():
                shutil.rmtree(out)
    if code != 0:
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
        "--metrics-json",
        type=Path,
        help="read the public metrics from this file instead of the GitHub API",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=DIST,
        help="where to build (default: dist/ in the repository)",
    )
    a = p.parse_args(argv)
    try:
        return build(a.release_json, a.out, a.metrics_json)
    except (OSError, BuildError) as e:
        print(f"build: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
