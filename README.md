# Pluryprint landing

The landing page of [Pluryprint](https://pluryprint.maecly.com), the desktop
app that prints many files at once on Windows and macOS. The app itself, its
releases and its issues live in
[MAECLY/pluryprint](https://github.com/MAECLY/pluryprint); this repository only
holds the static site and the few lines of Python that build it.

## What is here

- `site/` - the page as written: plain HTML, CSS and a little JavaScript, in
  Spanish (`index.html`) and English (`en/index.html`), with the screenshots,
  the share images, `llms.txt` and `agents.txt` for agents, the sitemap and
  the web manifests.
- `scripts/apply_release.py` - copies `site/` to an output folder with the
  release details filled in.
- `scripts/build.py` - the build command: fetches the latest release and runs
  `apply_release.py` into `dist/`.
- `vercel.json` - how Vercel builds and serves it.
- `tests/` - the checks below, run by `.github/workflows/ci.yml`.

## Release details

Nothing in `site/` names a version, a release date, a download link or a file
size. Those places carry `{{release.*}}` markers (`{{release.version}}`,
`{{release.windows_x64.url}}`, `{{release.apple_silicon.mib_en}}` and so on;
the full list is in the docstring of `scripts/apply_release.py`), and every
build fills them from the GitHub API record of the latest published release
of MAECLY/pluryprint.

The build fails, and nothing is deployed, when that release is a draft or a
pre-release, its tag is not `v<SemVer>`, any of the four installers or
`SHA256SUMS` is missing, empty or not where the page would link it, a marker
is unknown or left over, a download is linked nowhere, or a JSON file or
JSON-LD block no longer parses. There is no flag to skip any of that: the
landing never links a file that is not there.

## Deployment

The repository is connected to Vercel through its Git integration:

- a push to `main` deploys to production, https://pluryprint.maecly.com;
- a pull request gets a preview deployment of its own.

Vercel runs `python3 -I scripts/build.py` (no install step, nothing to
install) and serves `dist/`. If the build fails, the live site keeps its
previous deployment.

A new release of the app has to reach the page without a commit here, so
MAECLY/pluryprint's `landing.yml` workflow calls a Vercel Deploy Hook for
`main` whenever a release is published, and that rebuilds the page with the
new release.

`build.py` reads the release without a token, which GitHub allows 60 times an
hour per address. If Vercel's shared build machines ever hit that limit, set
a `GITHUB_TOKEN` environment variable in the Vercel project (a fine-grained
token with no permissions is enough to read a public release); the build
sends it as a bearer token and never prints it.

## Building and previewing locally

Python 3.9 or later, nothing else:

```sh
python3 scripts/build.py                  # the latest release, into dist/
python3 -m http.server -d dist 8000       # then open http://localhost:8000/
```

`python3 scripts/build.py --release-json tests/fixtures/release.json` builds
from a made-up release instead of the API, which works offline. `dist/` is
ignored by git and emptied by every build.

`http.server` does not redirect `/en` to `/en/` the way Vercel does
(`trailingSlash` in `vercel.json`), so open the English page as `/en/`.

## Tests

```sh
python3 -I -m unittest discover -s tests -v
```

They check that `site/` hardcodes no release and carries every marker, that a
fixture release fills every marker, that every release or marker the page
cannot link is refused, and that `vercel.json` and `build.py` build `dist/`
and fail on HTTP errors, a network that stays down or a release without an
installer. Nothing touches the network.

One test compares what `site/llms.txt` says about installing a missing
converter with the app's own dialog. It needs the app's interface copy, so it
is skipped unless `PLURYPRINT_APP_DIR` points at a checkout of
MAECLY/pluryprint:

```sh
PLURYPRINT_APP_DIR=../pluryprint python3 -I -m unittest discover -s tests -v
```

CI checks out the app's `src/i18n` for it, and there the test fails instead
of skipping if the variable is missing.

## License

The code is licensed under the GNU Affero General Public License v3.0 (see
`LICENSE`). The licence grants no rights to the Pluryprint name, wordmark or
Abanico mark beyond describing where the work came from; `TRADEMARKS.md` has
the notice. It is the app repository's file, so the paths it lists are in
MAECLY/pluryprint. The Fraunces font in `site/assets/fonts/` is under the SIL
Open Font License (`site/assets/fonts/OFL.txt`).
