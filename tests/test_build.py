"""The Vercel pipeline: vercel.json runs scripts/build.py, which fetches the
latest release, fills site/ into dist/ and fails the build on anything the
landing cannot link. No test here touches the network: urlopen is mocked."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from support import BASE, ROOT, SCRIPTS, fixture, load_script, read, site_texts

SECRET = "ghp_not-a-real-token-0123456789"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, reason: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/", code, reason, {}, io.BytesIO(b"{}")
    )


class VercelConfig(unittest.TestCase):
    def test_vercel_json_builds_the_landing_with_build_py(self):
        config = json.loads(read("vercel.json"))
        self.assertEqual(config["$schema"], "https://openapi.vercel.sh/vercel.json")
        # A static site: no framework preset and nothing to install (an empty
        # installCommand skips the step), only the build script.
        self.assertIsNone(config["framework"])
        self.assertEqual(config["installCommand"], "")
        self.assertEqual(config["buildCommand"], "python3 -I scripts/build.py")
        # The filled copy is what Vercel serves, never site/ with its markers.
        self.assertEqual(config["outputDirectory"], "dist")
        # site/en/index.html links ../assets/, so /en has to redirect to /en/
        # as it did on GitHub Pages.
        self.assertIs(config["trailingSlash"], True)
        self.assertEqual(
            set(config),
            {
                "$schema",
                "framework",
                "installCommand",
                "buildCommand",
                "outputDirectory",
                "trailingSlash",
            },
        )

    def test_build_py_fills_site_into_dist(self):
        build = load_script("build")
        self.assertEqual(build.ROOT, ROOT)
        self.assertEqual(build.SITE, ROOT / "site")
        self.assertEqual(build.DIST, ROOT / json.loads(read("vercel.json"))["outputDirectory"])
        self.assertEqual(build.REPO, "MAECLY/pluryprint")
        self.assertEqual(
            build.API, "https://api.github.com/repos/MAECLY/pluryprint/releases/latest"
        )
        self.assertNotEqual(build.DIST, build.SITE)
        self.assertIn("dist/", read(".gitignore").splitlines())

    def test_nothing_turns_the_checks_off(self):
        sources = {
            "vercel.json": read("vercel.json"),
            "scripts/build.py": read("scripts/build.py"),
            "scripts/apply_release.py": read("scripts/apply_release.py"),
        }
        for name, text in sources.items():
            for bypass in ["|| true", "||true", "continue-on-error"]:
                self.assertNotIn(bypass, text, f"{name}: {bypass}")
            # No option that skips or allows anything.
            flags = re.findall(r"add_argument\(\s*\"(--[a-z-]+)\"", text)
            for flag in flags:
                self.assertFalse(
                    flag.startswith(("--skip", "--allow", "--force", "--no-")),
                    f"{name}: {flag}",
                )
        self.assertNotIn("||", json.loads(sources["vercel.json"])["buildCommand"])
        self.assertNotIn(";", json.loads(sources["vercel.json"])["buildCommand"])


class Build(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.build = load_script("build")
        # No pauses between retries in the tests.
        self.build.RETRY_DELAYS = (0, 0)
        # The tests choose whether a token is set.
        env = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *args: str):
        """Runs build.py's main; returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.build.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def release_file(self, release: dict) -> Path:
        path = self.tmp / "release.json"
        path.write_text(json.dumps(release), encoding="utf-8")
        return path

    def assert_filled(self, out: Path):
        for file, text in site_texts(out):
            self.assertNotIn("{{", text, f"{file} keeps a marker")
        for page in ["index.html", "en/index.html", "404.html"]:
            self.assertTrue((out / page).is_file(), page)
        self.assertIn(
            f"{BASE}/download/v9.8.7/Pluryprint_9.8.7_windows_x64.exe",
            (out / "index.html").read_text(encoding="utf-8"),
        )
        # Only the site is published, not the repository around it.
        for name in ["CNAME", ".nojekyll", "vercel.json", "scripts", "tests"]:
            self.assertFalse((out / name).exists(), name)

    def test_build_fills_dist_from_a_release_file(self):
        out = self.tmp / "dist"
        code, stdout, stderr = self.main(
            "--release-json", str(self.release_file(fixture())), "--out", str(out)
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("Pluryprint 9.8.7 (2027-03-05)", stdout)
        self.assert_filled(out)

    def test_build_empties_dist_before_filling_it(self):
        dist = self.tmp / "dist"
        (dist / "old").mkdir(parents=True)
        (dist / "old/stale.html").write_text("{{release.version}}", encoding="utf-8")
        self.build.DIST = dist
        code, _, stderr = self.main("--release-json", str(self.release_file(fixture())))
        self.assertEqual(code, 0, stderr)
        self.assertFalse((dist / "old").exists())
        self.assert_filled(dist)

    def test_build_leaves_any_other_non_empty_folder_alone(self):
        out = self.tmp / "elsewhere"
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
        code, _, stderr = self.main(
            "--release-json", str(self.release_file(fixture())), "--out", str(out)
        )
        self.assertNotEqual(code, 0)
        self.assertIn("is not empty", stderr)
        self.assertEqual((out / "keep.txt").read_text(encoding="utf-8"), "mine")

    def test_build_fails_on_a_release_missing_an_installer(self):
        release = fixture()
        release["assets"] = [
            a for a in release["assets"] if a["name"] != "Pluryprint_9.8.7_apple_intel.dmg"
        ]
        dist = self.tmp / "dist"
        self.build.DIST = dist
        code, _, stderr = self.main("--release-json", str(self.release_file(release)))
        self.assertNotEqual(code, 0)
        self.assertIn("Pluryprint_9.8.7_apple_intel.dmg", stderr)
        self.assertIn("build: ", stderr)
        # Half a build is not left behind.
        self.assertFalse(dist.exists())

    def test_build_fetches_the_latest_release_without_a_token(self):
        body = json.dumps(fixture()).encode()
        out = self.tmp / "dist"
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(body)
        ) as urlopen:
            code, _, stderr = self.main("--out", str(out))
        self.assertEqual(code, 0, stderr)
        self.assert_filled(out)
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, self.build.API)
        self.assertEqual(request.get_method(), "GET")
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers["accept"], "application/vnd.github+json")
        self.assertIn("x-github-api-version", headers)
        self.assertTrue(headers["user-agent"])
        self.assertNotIn("authorization", headers)
        self.assertIn("timeout", urlopen.call_args[1])

    def test_build_sends_a_token_when_set_and_never_prints_it(self):
        for name in ["GITHUB_TOKEN", "GH_TOKEN"]:
            with self.subTest(name), mock.patch.dict(os.environ, {name: SECRET}):
                with mock.patch(
                    "urllib.request.urlopen", side_effect=http_error(500, "Server Error")
                ) as urlopen:
                    code, stdout, stderr = self.main("--out", str(self.tmp / name))
                self.assertNotEqual(code, 0)
                request = urlopen.call_args[0][0]
                headers = {k.lower(): v for k, v in request.header_items()}
                self.assertEqual(headers["authorization"], f"Bearer {SECRET}")
                # Not copied to wherever a redirect would point.
                self.assertNotIn(
                    "authorization", {k.lower() for k in request.headers}
                )
                self.assertNotIn(SECRET, stdout + stderr)

    def test_build_fails_on_http_errors(self):
        for code_, reason, tries in [
            (404, "Not Found", 1),
            (403, "Forbidden", 1),
            (500, "Internal Server Error", 3),
            (503, "Service Unavailable", 3),
        ]:
            with self.subTest(code_):
                dist = self.tmp / f"dist{code_}"
                self.build.DIST = dist
                with mock.patch(
                    "urllib.request.urlopen", side_effect=http_error(code_, reason)
                ) as urlopen:
                    code, _, stderr = self.main()
                self.assertNotEqual(code, 0)
                self.assertIn(f"answered {code_}", stderr)
                # 4xx is final; 5xx is asked again, but not forever.
                self.assertEqual(urlopen.call_count, tries)
                self.assertFalse(dist.exists())

    def test_build_retries_a_network_error_then_succeeds(self):
        body = json.dumps(fixture()).encode()
        out = self.tmp / "dist"
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=[urllib.error.URLError("connection reset"), FakeResponse(body)],
        ) as urlopen:
            code, _, stderr = self.main("--out", str(out))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("retrying", stderr)
        self.assert_filled(out)

    def test_build_fails_when_the_network_stays_down(self):
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("no route")
        ) as urlopen:
            code, _, stderr = self.main("--out", str(self.tmp / "dist"))
        self.assertNotEqual(code, 0)
        self.assertEqual(urlopen.call_count, 3)
        self.assertIn("no route", stderr)

    def test_build_fails_on_an_answer_that_is_not_json(self):
        with mock.patch(
            "urllib.request.urlopen", return_value=FakeResponse(b"<html>oops</html>")
        ):
            code, _, stderr = self.main("--out", str(self.tmp / "dist"))
        self.assertNotEqual(code, 0)
        self.assertIn("no JSON", stderr)

    def test_build_fails_on_json_that_is_not_a_release_object(self):
        for body in [b"[]", b"null"]:
            with self.subTest(body=body):
                dist = self.tmp / "dist"
                self.build.DIST = dist
                with mock.patch(
                    "urllib.request.urlopen", return_value=FakeResponse(body)
                ):
                    code, _, stderr = self.main()
                self.assertNotEqual(code, 0)
                self.assertIn("no JSON object", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertFalse(dist.exists())

    def test_build_removes_dist_when_apply_release_crashes(self):
        dist = self.tmp / "dist"
        self.build.DIST = dist
        real = self.build.load_apply_release

        def crashing():
            module = real()
            original = module.apply

            def apply(release, repo, site, out):
                original(release, repo, site, out)
                raise RuntimeError("crash after filling")

            module.apply = apply
            return module

        self.build.load_apply_release = crashing
        with self.assertRaises(RuntimeError):
            self.main("--release-json", str(self.release_file(fixture())))
        self.assertFalse(dist.exists())

    def test_build_runs_from_any_folder(self):
        # Paths come from the script's location, not the current folder.
        out = self.tmp / "dist"
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, _, stderr = self.main(
                "--release-json", str(self.release_file(fixture())), "--out", str(out)
            )
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, stderr)
        self.assert_filled(out)


class Ci(unittest.TestCase):
    def test_ci_runs_the_tests_and_an_offline_build(self):
        ci = read(".github/workflows/ci.yml")
        self.assertIn("python3 -I -m unittest discover -s tests -v", ci)
        self.assertIn("PLURYPRINT_APP_DIR:", ci)
        # The same fixture the tests use, so CI never needs the API.
        self.assertIn(
            "python3 -I scripts/build.py --release-json tests/fixtures/release.json",
            ci,
        )
        self.assertTrue((SCRIPTS / "build.py").is_file())
        # Least privilege, and no token left in the checkouts.
        self.assertIn("\npermissions: {}\n", ci)
        self.assertNotIn("write", ci)
        self.assertEqual(ci.count("persist-credentials: false"), ci.count("actions/checkout@"))
        for bypass in ["continue-on-error", "|| true"]:
            self.assertNotIn(bypass, ci)


if __name__ == "__main__":
    unittest.main()
