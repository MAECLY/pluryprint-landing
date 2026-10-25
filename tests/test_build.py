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


API_ROOT = "https://api.github.com/repos/MAECLY/pluryprint"


def releases_page(downloads: list[int]) -> list[dict]:
    """A page of releases whose assets were downloaded these many times."""
    return [
        {"tag_name": f"v0.0.{i}", "assets": [{"download_count": n}]}
        for i, n in enumerate(downloads)
    ]


class FakeApi:
    """urlopen for the three API calls the build makes, routed by URL.

    pages: the bodies of /releases?page=1, 2...; repo: /repos/OWNER/NAME.
    Each can be an exception to raise instead. Every request is kept.
    """

    def __init__(self, pages=None, repo=None, latest=None):
        self.latest = fixture() if latest is None else latest
        self.pages = [releases_page([40, 27])] if pages is None else pages
        self.repo = {"stargazers_count": 0} if repo is None else repo
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        if url == f"{API_ROOT}/releases/latest":
            body = self.latest
        elif url.startswith(f"{API_ROOT}/releases?"):
            page = int(url.rsplit("page=", 1)[1])
            body = self.pages[page - 1] if page <= len(self.pages) else []
        elif url == API_ROOT:
            body = self.repo
        else:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(body, Exception):
            raise body
        return FakeResponse(body if isinstance(body, bytes) else json.dumps(body).encode())

    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]


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


class BuildCase(unittest.TestCase):
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


class Build(BuildCase):
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
        request = urlopen.call_args_list[0][0][0]
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
        api = FakeApi()
        responses = [urllib.error.URLError("connection reset")]

        def flaky(request, timeout=None):
            if responses:
                raise responses.pop()
            return api(request, timeout)

        with mock.patch("urllib.request.urlopen", side_effect=flaky) as urlopen:
            code, _, stderr = self.main("--out", str(out))
        self.assertEqual(code, 0, stderr)
        # The failed try and the retry of the release, then the two metrics.
        self.assertEqual(urlopen.call_count, 4)
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

            def apply(*args):
                original(*args)
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


class Metrics(BuildCase):
    """The public metrics: shown only when the API gives a count above zero,
    never failing the build and never shown as a guess or a zero."""

    def built(self, api: FakeApi):
        out = self.tmp / f"dist{len(list(self.tmp.iterdir()))}"
        with mock.patch("urllib.request.urlopen", side_effect=api):
            code, stdout, stderr = self.main("--out", str(out))
        self.assertEqual(code, 0, stderr)
        self.assert_filled(out)
        es = (out / "index.html").read_text(encoding="utf-8")
        en = (out / "en/index.html").read_text(encoding="utf-8")
        return es, en, stdout, stderr

    @staticmethod
    def software(page: str) -> dict:
        ld = page.split('<script type="application/ld+json">', 1)[1]
        ld = json.loads(ld.split("</script>", 1)[0])
        return next(n for n in ld["@graph"] if n["@type"] == "SoftwareApplication")

    def assert_no_metrics(self, es: str, en: str):
        for page in [es, en]:
            self.assertNotIn("descarga", page.split("dl-links", 1)[1].split("</p>", 1)[0])
            self.assertNotIn(" in total", page)
            self.assertNotIn(" en total", page)
            self.assertNotIn("estrella", page)
            self.assertNotIn(" stars", page)
            self.assertNotIn("interactionStatistic", page)
            self.assertNotIn("metric", page)
        self.assertIn('>GitHub</a>.</p>', es)
        self.assertIn('>GitHub</a>.</p>', en)

    def test_build_shows_downloads_summed_over_every_release_and_page(self):
        api = FakeApi(
            pages=[releases_page([1000] * 99 + [200]), releases_page([30, 4])],
            repo={"stargazers_count": 0},
        )
        es, en, stdout, _ = self.built(api)
        self.assertIn(f"{self.build.RELEASES_API}?per_page=100&page=2", api.urls())
        self.assertIn(f"{self.build.RELEASES_API}?per_page=100&page=1", api.urls())
        # 99 * 1000 + 200 + 30 + 4, in each language's own way.
        self.assertIn(
            'Todas las versiones</a> · <span class="nobreak">99.234 descargas en total</span></p>',
            es,
        )
        self.assertIn(
            'All releases</a> · <span class="nobreak">99,234 downloads in total</span></p>',
            en,
        )
        for page in [es, en]:
            stat = self.software(page)["interactionStatistic"]
            self.assertEqual(stat["interactionType"], "https://schema.org/DownloadAction")
            self.assertEqual(stat["userInteractionCount"], 99234)
        # Zero stars: no star count anywhere.
        self.assertNotIn("estrella", es)
        self.assertNotIn(" stars", en)
        self.assertIn("downloads: 99234", stdout)

    def test_build_shows_stars_next_to_the_source_link(self):
        es, en, _, _ = self.built(FakeApi(repo={"stargazers_count": 1}))
        self.assertIn('>GitHub</a>, con 1 estrella.</p>', es)
        self.assertIn('>GitHub</a>, with 1 star.</p>', en)
        es, en, _, _ = self.built(FakeApi(repo={"stargazers_count": 1234}))
        self.assertIn('>GitHub</a>, con 1.234 estrellas.</p>', es)
        self.assertIn('>GitHub</a>, with 1,234 stars.</p>', en)
        self.assertIn("67 descargas en total", es)
        self.assertIn("67 downloads in total", en)

    def test_build_shows_no_metric_that_is_zero(self):
        es, en, _, _ = self.built(
            FakeApi(pages=[releases_page([0, 0])], repo={"stargazers_count": 0})
        )
        self.assert_no_metrics(es, en)
        es, en, _, _ = self.built(FakeApi(pages=[[]]))
        self.assert_no_metrics(es, en)

    def test_build_shows_no_metric_the_api_does_not_give(self):
        for why, api in [
            ("rate limited", FakeApi(
                pages=[http_error(403, "rate limit exceeded")],
                repo=http_error(403, "rate limit exceeded"),
            )),
            ("server error", FakeApi(
                pages=[http_error(502, "Bad Gateway")], repo=http_error(502, "Bad Gateway")
            )),
            ("network down", FakeApi(
                pages=[urllib.error.URLError("no route")],
                repo=urllib.error.URLError("no route"),
            )),
            ("not JSON", FakeApi(pages=[b"<html>"], repo=b"<html>")),
            ("wrong shape", FakeApi(pages=[{"message": "?"}], repo=[])),
            ("bad counts", FakeApi(
                pages=[[{"assets": [{"download_count": "67"}]}]],
                repo={"stargazers_count": None},
            )),
            ("negative", FakeApi(
                pages=[[{"assets": [{"download_count": -1}]}]],
                repo={"stargazers_count": -3},
            )),
        ]:
            with self.subTest(why):
                es, en, _, stderr = self.built(api)
                self.assert_no_metrics(es, en)
                self.assertIn("downloads unknown, the page shows none", stderr)
                self.assertIn("stars unknown, the page shows none", stderr)

    def test_a_later_page_failing_shows_no_partial_total(self):
        api = FakeApi(
            pages=[releases_page([1] * 100), http_error(500, "Server Error")],
            repo={"stargazers_count": 5},
        )
        es, en, _, stderr = self.built(api)
        self.assertNotIn("descargas en total", es)
        self.assertNotIn("downloads in total", en)
        self.assertIn("5 estrellas", es)
        self.assertIn("downloads unknown", stderr)

    def test_metrics_send_the_token_too(self):
        api = FakeApi()
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": SECRET}):
            _, _, stdout, stderr = self.built(api)
        self.assertEqual(len(api.requests), 3)
        for request in api.requests:
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers["authorization"], f"Bearer {SECRET}")
            self.assertNotIn("authorization", {k.lower() for k in request.headers})
        self.assertNotIn(SECRET, stdout + stderr)

    def test_an_offline_build_asks_for_no_metric(self):
        out = self.tmp / "dist"
        with mock.patch("urllib.request.urlopen") as urlopen:
            code, _, stderr = self.main(
                "--release-json", str(self.release_file(fixture())), "--out", str(out)
            )
        self.assertEqual(code, 0, stderr)
        urlopen.assert_not_called()
        self.assert_no_metrics(
            (out / "index.html").read_text(encoding="utf-8"),
            (out / "en/index.html").read_text(encoding="utf-8"),
        )

    def test_an_offline_build_shows_metrics_from_a_file(self):
        metrics = self.tmp / "metrics.json"
        metrics.write_text(json.dumps({"downloads": 208, "stars": 0}), encoding="utf-8")
        out = self.tmp / "dist"
        code, _, stderr = self.main(
            "--release-json", str(self.release_file(fixture())),
            "--metrics-json", str(metrics),
            "--out", str(out),
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("208 descargas en total", (out / "index.html").read_text(encoding="utf-8"))
        self.assertIn("208 downloads in total", (out / "en/index.html").read_text(encoding="utf-8"))


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
