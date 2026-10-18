"""site/ as committed: it names no release by hand, carries the markers that
matter, and tells agents what the app itself says."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from support import LABELS, ROOT, read, site_texts


class SiteSources(unittest.TestCase):
    def test_site_sources_link_no_release_by_hand(self):
        for file, text in site_texts(ROOT / "site"):
            for hard in [
                "/releases/download/v",
                "/releases/tag/v",
                "Pluryprint_0.",
                "0.0.1",
                "0.0.2",
            ]:
                self.assertNotIn(
                    hard,
                    text,
                    f"site/{file} hardcodes {hard!r}: use a {{{{release.*}}}} marker",
                )

        # Both landings and the agent files carry the markers that matter.
        for file in ["index.html", "en/index.html"]:
            text = read(f"site/{file}")
            for label, _ in LABELS:
                self.assertIn(
                    f'href="{{{{release.{label}.url}}}}"', text, f"{file} {label}"
                )
                self.assertIn(
                    f"{{{{release.{label}.mib_", text, f"{file} {label} size"
                )
            for m in [
                "{{release.version}}",
                "{{release.notes_url}}",
                "{{release.sha256sums_url}}",
                '"softwareVersion":"{{release.version}}"',
                '"datePublished":"{{release.date}}"',
                '"releaseNotes":"{{release.notes_url}}"',
            ]:
                self.assertIn(m, text, f"{file} lacks {m}")

        llms = read("site/llms.txt")
        for label, _ in LABELS:
            self.assertIn(
                f"{{{{release.{label}.url}}}} (about {{{{release.{label}.mib_en}}}} MiB)",
                llms,
            )
        self.assertIn("Release: {{release.notes_url}}", read("site/agents.txt"))

    def test_github_pages_leftovers_are_gone(self):
        # Vercel takes the domain from the project and runs no Jekyll.
        for name in ["CNAME", ".nojekyll"]:
            self.assertFalse((ROOT / "site" / name).exists(), name)

    def test_site_llms_txt_describes_the_install_consent_as_the_dialog_does(self):
        llms = read("site/llms.txt")
        consent = next(
            (l for l in llms.splitlines() if "offers to install it" in l), None
        )
        self.assertIsNotNone(consent, "llms.txt describes the install offer")
        self.assertNotIn("the price", consent)
        self.assertIn("may cost money in the Microsoft Store", consent)

        # The dialog's wording lives in the app repository.
        app = os.environ.get("PLURYPRINT_APP_DIR", "").strip()
        if not app:
            # CI checks out the app for this, so there a missing variable is a
            # broken workflow, not a reason to skip.
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail("PLURYPRINT_APP_DIR is not set in CI")
            self.skipTest(
                "PLURYPRINT_APP_DIR is not set: point it at a checkout of"
                " MAECLY/pluryprint to compare with the app's install dialog"
            )
        path = Path(app) / "src/i18n/locales/en/printJob.json"
        dialog = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            dialog["install"]["paid"],
            "This component may cost money in the Microsoft Store.",
        )


if __name__ == "__main__":
    unittest.main()
