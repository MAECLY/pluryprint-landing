"""scripts/apply_release.py: fills every marker from a release, and refuses a
release, or a site/, the landing cannot link."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import BASE, LABELS, ROOT, fixture, read, run_apply_release, site_texts


class Workdir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def apply(self, release: dict, site: Path = ROOT / "site", n: int = 0):
        """Runs the script on release; returns the process and its output folder."""
        release_json = self.tmp / f"release{n}.json"
        release_json.write_text(json.dumps(release), encoding="utf-8")
        out = self.tmp / f"out{n}"
        return run_apply_release(release_json, site, out), out


class FillsTheRelease(Workdir):
    def test_apply_release_fills_every_marker_from_the_release(self):
        proc, filled = self.apply(fixture())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for file, text in site_texts(filled):
            self.assertNotIn("{{", text, f"{file} keeps a marker")
            self.assertNotIn("0.0.1", text, file)

        # Files without markers are copied as they are.
        for rel in ["robots.txt", "assets/site.css", "favicon.ico"]:
            self.assertEqual(
                (filled / rel).read_bytes(), (ROOT / "site" / rel).read_bytes(), rel
            )
        self.assertTrue((filled / "assets").is_dir())

        es = (filled / "index.html").read_text(encoding="utf-8")
        en = (filled / "en/index.html").read_text(encoding="utf-8")
        for i, (label, ext) in enumerate(LABELS):
            href = f'href="{BASE}/download/v9.8.7/Pluryprint_9.8.7_{label}.{ext}"'
            self.assertIn(href, es)
            self.assertIn(href, en)
            self.assertIn(f"{10 + i},1&nbsp;MiB", es, f"es {label}")
            self.assertIn(f"{10 + i}.1&nbsp;MiB", en, f"en {label}")
        for page in [es, en]:
            self.assertIn(f'href="{BASE}/download/v9.8.7/SHA256SUMS"', page)
            self.assertIn(f'href="{BASE}/tag/v9.8.7"', page)
            self.assertIn("v9.8.7 preview", page)
            # The JSON-LD still parses and names the release.
            ld = page.split('<script type="application/ld+json">', 1)[1]
            ld = json.loads(ld.split("</script>", 1)[0])
            app = next(
                n for n in ld["@graph"] if n["@type"] == "SoftwareApplication"
            )
            self.assertEqual(app["softwareVersion"], "9.8.7")
            self.assertEqual(app["datePublished"], "2027-03-05")
            self.assertEqual(app["releaseNotes"], f"{BASE}/tag/v9.8.7")
            self.assertEqual(len(app["downloadUrl"]), 4)
        self.assertIn("5 de marzo de 2027", es)
        self.assertIn("5 March 2027", en)

        llms = (filled / "llms.txt").read_text(encoding="utf-8")
        self.assertIn(
            f"{BASE}/download/v9.8.7/Pluryprint_9.8.7_apple_intel.dmg (about 13.1 MiB)",
            llms,
        )
        agents = (filled / "agents.txt").read_text(encoding="utf-8")
        self.assertIn("version 9.8.7 preview, published 2027-03-05", agents)
        for manifest in ["site.webmanifest", "en/site.webmanifest"]:
            m = json.loads((filled / manifest).read_text(encoding="utf-8"))
            self.assertIn("9.8.7", m["description"], manifest)
        # site/ itself is left alone.
        self.assertIn("{{release.windows_x64.url}}", read("site/index.html"))


class RefusesARelease(Workdir):
    """A release the landing cannot link fails the build instead."""

    def refused(self, mutate, why: str) -> str:
        release = fixture()
        mutate(release)
        proc, _ = self.apply(release)
        self.assertNotEqual(proc.returncode, 0, f"{why}: {proc.stdout}")
        self.assertTrue(
            proc.stderr.startswith("apply_release: "), f"{why}: {proc.stderr}"
        )
        return proc.stderr

    @staticmethod
    def drop_asset(name: str):
        def mutate(r: dict) -> None:
            r["assets"] = [a for a in r["assets"] if a["name"] != name]

        return mutate

    def test_apply_release_refuses_a_release_the_landing_cannot_link(self):
        for label, ext in LABELS:
            name = f"Pluryprint_9.8.7_{label}.{ext}"
            with self.subTest(missing=name):
                self.assertIn(name, self.refused(self.drop_asset(name), name))

        def setter(*path_and_value):
            *path, value = path_and_value

            def mutate(r: dict) -> None:
                target = r
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value

            return mutate

        cases = [
            (self.drop_asset("SHA256SUMS"), "no SHA256SUMS"),
            (setter("assets", 0, "size", 0), "empty installer"),
            (setter("assets", 1, "state", "starter"), "upload not finished"),
            (
                setter(
                    "assets", 2, "browser_download_url", "https://example.com/x.dmg"
                ),
                "download elsewhere",
            ),
            (setter("prerelease", True), "pre-release"),
            (setter("draft", True), "draft"),
            (setter("tag_name", "nightly"), "not a version tag"),
            (setter("html_url", "https://example.com/"), "notes elsewhere"),
            (setter("published_at", "yesterday"), "no date"),
        ]
        for mutate, why in cases:
            with self.subTest(why):
                self.refused(mutate, why)

    def test_apply_release_refuses_a_release_that_is_not_an_object(self):
        # An array or null fails with a message, not a traceback.
        for release in [[], None]:
            with self.subTest(release=release):
                proc, out = self.apply(release)
                self.assertNotEqual(proc.returncode, 0)
                self.assertTrue(
                    proc.stderr.startswith("apply_release: "), proc.stderr
                )
                self.assertIn("not an object", proc.stderr)
                self.assertFalse(out.exists())


class RefusesMarkers(Workdir):
    """A marker the script does not know, a malformed one, or a missing
    download fails the build instead of reaching the page."""

    def run_page(self, page: str, n: int):
        site = self.tmp / f"site{n}"
        site.mkdir()
        (site / "index.html").write_text(page, encoding="utf-8")
        proc, _ = self.apply(fixture(), site=site, n=n)
        return proc

    def test_apply_release_refuses_unknown_or_missing_markers(self):
        all_links = "".join(f"{{{{release.{l}.url}}}}\n" for l, _ in LABELS)
        ok = self.run_page(all_links, 0)
        self.assertEqual(ok.returncode, 0, ok.stderr)

        unknown = self.run_page(f"{all_links}{{{{release.windows_x86.url}}}}", 1)
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("unknown marker", unknown.stderr)

        malformed = self.run_page(f"{all_links}{{{{release.version}}", 2)
        self.assertNotEqual(malformed.returncode, 0)

        missing = self.run_page("{{release.windows_x64.url}}", 3)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("never links", missing.stderr)

        bad_ld = self.run_page(
            f'{all_links}<script type="application/ld+json">'
            '{"v":{{release.version}}}</script>',
            4,
        )
        self.assertNotEqual(bad_ld.returncode, 0)
        self.assertIn("valid JSON", bad_ld.stderr)


if __name__ == "__main__":
    unittest.main()
