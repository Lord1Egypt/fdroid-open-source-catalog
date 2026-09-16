# F-Droid open-source app catalog

[![Update app catalog](https://github.com/Lord1Egypt/fdroid-open-source-catalog/actions/workflows/update-catalog.yml/badge.svg)](https://github.com/Lord1Egypt/fdroid-open-source-catalog/actions/workflows/update-catalog.yml)

`fdroid_catalog.py` downloads public F-Droid-compatible repository indexes and
creates a local, searchable catalog of Android apps and their source-code links.
It uses only Python's standard library; Python 3.10 or newer is recommended.

By default it collects:

- F-Droid
- IzzyOnDroid
- Guardian Project
- microG

## Run

```bash
python3 fdroid_catalog.py
```

The files are written to `output/`:

- `apps.json` — complete structured metadata
- `apps.md` — readable Markdown table
- `apps.txt` — tab-separated plain text, convenient for `grep` or spreadsheets

Search the generated JSON without accessing the network:

```bash
python3 fdroid_catalog.py --search "password manager"
python3 fdroid_catalog.py --search github.com
```

Use one or more custom F-Droid-compatible repositories (these replace the
built-in defaults):

```bash
python3 fdroid_catalog.py \
  --repo "My repo=https://example.org/fdroid/repo" \
  --repo "Another=https://repo.example.net"
```

Both modern `index-v2.json` and legacy `index-v1.json` repositories are
supported. A direct index URL can also be supplied.

## Optional GitHub enrichment

Every GitHub source URL is normalized into owner/repository fields without API
access. To additionally fetch stars, language, license, topics, activity dates,
and archive status from GitHub:

```bash
GITHUB_TOKEN="your_token" python3 fdroid_catalog.py --enrich-github --github-limit 0
```

Without a token GitHub's public API has a low rate limit, so the default is at
most 50 unique projects. The token is read only from the `GITHUB_TOKEN`
environment variable and is never saved in the output.

## Useful options

```text
--output-dir DIR       choose the output directory
--locale LOCALE        preferred locale (default: en-US)
--workers N            parallel repository downloads (default: 4)
--timeout SECONDS      per-request timeout (default: 60)
--max-download-mb N    safety limit for each repository index (default: 150)
```

The script records repository download failures in `apps.json` and continues
with repositories that remain available. Repository indexes are metadata and
are not cryptographically verified by this cataloging tool; use an F-Droid
client for trusted APK installation.

## Automatic weekly updates

The GitHub Actions workflow in `.github/workflows/update-catalog.yml` runs every
Monday at 03:17 UTC. It runs the tests, rebuilds all three catalog formats, and
commits changed results using the GitHub Actions bot. It can also be started
manually from the repository's **Actions** tab.
