#!/usr/bin/env python3
"""Collect searchable app/source metadata from F-Droid-compatible repositories."""

from __future__ import annotations

import argparse
import concurrent.futures
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


VERSION = "1.0.0"
USER_AGENT = f"fdroid-catalog/{VERSION} (+local open-source metadata collector)"
DEFAULT_REPOS = (
    ("F-Droid", "https://f-droid.org/repo"),
    ("IzzyOnDroid", "https://apt.izzysoft.de/fdroid/repo"),
    ("Guardian Project", "https://guardianproject.info/fdroid/repo"),
    ("microG", "https://microg.org/fdroid/repo"),
)


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"br", "p", "li", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "li", "div"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", html.unescape("".join(self.parts))).strip()


def plain_text(value: Any) -> str:
    if value is None:
        return ""
    parser = TextExtractor()
    parser.feed(str(value))
    return parser.text()


def localized(value: Any, locale: str = "en-US") -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict) or not value:
        return ""
    choices = (locale, locale.split("-", 1)[0], "en-US", "en")
    for key in choices:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for key in sorted(value):
        candidate = value[key]
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def clean_url(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def timestamp_iso(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 10_000_000_000:
        number /= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def github_project(urls: Iterable[str]) -> tuple[str, str, str]:
    """Return canonical URL, owner and repository from the first GitHub URL."""
    for raw_url in urls:
        if not raw_url:
            continue
        candidate = raw_url if "://" in raw_url else "https://" + raw_url
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if host not in {"github.com", "www.github.com"}:
            continue
        pieces = [piece for piece in parsed.path.split("/") if piece]
        if len(pieces) < 2:
            continue
        owner = pieces[0]
        repo = pieces[1]
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if owner and repo:
            return f"https://github.com/{owner}/{repo}", owner, repo
    return "", "", ""


def newest_version(versions: Any) -> dict[str, Any]:
    if not isinstance(versions, dict) or not versions:
        return {}

    def sort_key(version: Any) -> tuple[int, int]:
        if not isinstance(version, dict):
            return (-1, -1)
        manifest = version.get("manifest") or {}
        try:
            version_code = int(manifest.get("versionCode", -1))
        except (TypeError, ValueError):
            version_code = -1
        try:
            added = int(version.get("added", -1))
        except (TypeError, ValueError):
            added = -1
        return version_code, added

    candidate = max(versions.values(), key=sort_key)
    return candidate if isinstance(candidate, dict) else {}


def make_record(
    package_id: str,
    metadata: dict[str, Any],
    version: dict[str, Any],
    repo_name: str,
    repo_url: str,
    locale: str,
) -> dict[str, Any]:
    manifest = version.get("manifest") or {}
    source_url = clean_url(metadata.get("sourceCode"))
    website = clean_url(metadata.get("webSite"))
    issues = clean_url(metadata.get("issueTracker"))
    changelog = clean_url(metadata.get("changelog"))
    github_url, github_owner, github_repo = github_project(
        (source_url, issues, changelog, website)
    )
    source_host = (urlparse(source_url).hostname or "").lower() if source_url else ""
    anti_features = set()
    for source in (metadata.get("antiFeatures"), version.get("antiFeatures")):
        if isinstance(source, dict):
            anti_features.update(str(key) for key in source)
        elif isinstance(source, list):
            anti_features.update(str(item) for item in source)

    categories = metadata.get("categories") or []
    if not isinstance(categories, list):
        categories = [categories]
    try:
        version_code: int | str = int(manifest.get("versionCode"))
    except (TypeError, ValueError):
        version_code = manifest.get("versionCode") or ""

    return {
        "package_id": package_id,
        "name": plain_text(localized(metadata.get("name"), locale)) or package_id,
        "summary": plain_text(localized(metadata.get("summary"), locale)),
        "description": plain_text(localized(metadata.get("description"), locale)),
        "author": plain_text(metadata.get("authorName") or ""),
        "license": str(metadata.get("license") or ""),
        "categories": sorted({str(item) for item in categories if item}),
        "anti_features": sorted(anti_features),
        "website": website,
        "source_url": source_url,
        "source_host": source_host,
        "issue_tracker": issues,
        "changelog": changelog,
        "github_url": github_url,
        "github_owner": github_owner,
        "github_repo": github_repo,
        "latest_version": str(manifest.get("versionName") or ""),
        "latest_version_code": version_code,
        "min_sdk": (manifest.get("usesSdk") or {}).get("minSdkVersion", ""),
        "target_sdk": (manifest.get("usesSdk") or {}).get("targetSdkVersion", ""),
        "added": timestamp_iso(metadata.get("added")),
        "last_updated": timestamp_iso(metadata.get("lastUpdated") or version.get("added")),
        "repositories": [{"name": repo_name, "url": repo_url}],
    }


def parse_v2(data: dict[str, Any], fallback_name: str, repo_url: str, locale: str) -> tuple[str, list[dict[str, Any]]]:
    repo_name = localized((data.get("repo") or {}).get("name"), locale) or fallback_name
    packages = data.get("packages")
    if not isinstance(packages, dict):
        raise ValueError("index-v2.json has no 'packages' object")
    records = []
    for package_id, package in packages.items():
        if not isinstance(package, dict):
            continue
        metadata = package.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        records.append(
            make_record(
                str(package_id), metadata, newest_version(package.get("versions")),
                repo_name, repo_url, locale,
            )
        )
    return repo_name, records


def parse_v1(data: dict[str, Any], fallback_name: str, repo_url: str, locale: str) -> tuple[str, list[dict[str, Any]]]:
    repo = data.get("repo") or {}
    repo_name = localized(repo.get("name"), locale) or fallback_name
    apps = data.get("apps")
    if not isinstance(apps, list):
        raise ValueError("index-v1.json has no 'apps' array")
    records = []
    for app in apps:
        if not isinstance(app, dict):
            continue
        package_id = str(app.get("packageName") or app.get("package_id") or "")
        if not package_id:
            continue
        localized_data = app.get("localized")
        localized_name = ""
        if isinstance(localized_data, dict):
            locale_data = localized_data.get(locale)
            if isinstance(locale_data, dict):
                localized_name = locale_data.get("name") or ""
        metadata = {
            "name": localized_name,
            "summary": app.get("summary"),
            "description": app.get("description"),
            "authorName": app.get("authorName"),
            "license": app.get("license"),
            "categories": app.get("categories"),
            "antiFeatures": app.get("antiFeatures"),
            "webSite": app.get("webSite"),
            "sourceCode": app.get("sourceCode"),
            "issueTracker": app.get("issueTracker"),
            "changelog": app.get("changelog"),
            "added": app.get("added"),
            "lastUpdated": app.get("lastUpdated"),
        }
        if not metadata["name"]:
            metadata["name"] = app.get("name")
        version = {
            "manifest": {
                "versionName": app.get("suggestedVersionName"),
                "versionCode": app.get("suggestedVersionCode"),
            }
        }
        records.append(make_record(package_id, metadata, version, repo_name, repo_url, locale))
    return repo_name, records


def index_candidates(repo_url: str) -> list[tuple[str, str]]:
    base = repo_url.rstrip("/")
    if base.lower().endswith(".json"):
        version = "v1" if base.lower().endswith("index-v1.json") else "v2"
        return [(base, version)]
    return [(base + "/index-v2.json", "v2"), (base + "/index-v1.json", "v1")]


def download_json(url: str, timeout: float, max_bytes: int) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if length and int(length) > max_bytes:
            raise ValueError(f"download is larger than {max_bytes // 1_000_000} MB")
        chunks: list[bytes] = []
        downloaded = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            if downloaded > max_bytes:
                raise ValueError(f"download exceeded {max_bytes // 1_000_000} MB")
            chunks.append(chunk)
    result = json.loads(b"".join(chunks))
    if not isinstance(result, dict):
        raise ValueError("top-level JSON value is not an object")
    return result


def fetch_repository(name: str, repo_url: str, locale: str, timeout: float, max_bytes: int) -> dict[str, Any]:
    errors = []
    started = time.monotonic()
    for index_url, version in index_candidates(repo_url):
        try:
            data = download_json(index_url, timeout, max_bytes)
            parsed_name, records = (
                parse_v1(data, name, repo_url, locale)
                if version == "v1"
                else parse_v2(data, name, repo_url, locale)
            )
            return {
                "name": parsed_name,
                "url": repo_url,
                "index_url": index_url,
                "index_version": version,
                "status": "ok",
                "app_count": len(records),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "records": records,
            }
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{index_url}: {error}")
    return {
        "name": name,
        "url": repo_url,
        "status": "error",
        "error": " | ".join(errors),
        "app_count": 0,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "records": [],
    }


def merge_records(repository_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    list_fields = {"categories", "anti_features"}
    version_fields = {"latest_version", "latest_version_code", "min_sdk", "target_sdk", "last_updated"}
    for result in repository_results:
        for record in result["records"]:
            package_id = record["package_id"]
            current = merged.get(package_id)
            if current is None:
                merged[package_id] = record
                continue
            current["repositories"].extend(
                repo for repo in record["repositories"] if repo not in current["repositories"]
            )
            for field in list_fields:
                current[field] = sorted(set(current[field]) | set(record[field]))
            for field, value in record.items():
                if field in list_fields or field == "repositories":
                    continue
                if not current.get(field) and value:
                    current[field] = value
            try:
                newer = int(record.get("latest_version_code") or -1) > int(current.get("latest_version_code") or -1)
            except (TypeError, ValueError):
                newer = record.get("last_updated", "") > current.get("last_updated", "")
            if newer:
                for field in version_fields:
                    current[field] = record.get(field, "")
    return sorted(merged.values(), key=lambda item: (item["name"].casefold(), item["package_id"]))


def github_api_metadata(owner: str, repo: str, token: str, timeout: float) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    license_data = data.get("license") or {}
    return {
        "full_name": data.get("full_name"),
        "description": data.get("description"),
        "homepage": data.get("homepage"),
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
        "language": data.get("language"),
        "license": license_data.get("spdx_id"),
        "topics": data.get("topics") or [],
        "default_branch": data.get("default_branch"),
        "archived": data.get("archived"),
        "fork": data.get("fork"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "pushed_at": data.get("pushed_at"),
    }


def enrich_github(records: list[dict[str, Any]], limit: int, timeout: float) -> tuple[int, list[str]]:
    projects: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record["github_owner"] and record["github_repo"]:
            projects.setdefault((record["github_owner"], record["github_repo"]), []).append(record)
    items = sorted(projects.items())
    if limit > 0:
        items = items[:limit]
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    errors: list[str] = []
    enriched = 0
    for (owner, repo), related_records in items:
        try:
            metadata = github_api_metadata(owner, repo, token, timeout)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            errors.append(f"{owner}/{repo}: {error}")
            if isinstance(error, HTTPError) and error.code in {403, 429}:
                break
            continue
        for record in related_records:
            record["github_metadata"] = metadata
        enriched += 1
    return enriched, errors


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def markdown_escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(catalog: dict[str, Any]) -> str:
    lines = [
        "# Open-source Android app catalog",
        "",
        f"Generated: {catalog['generated_at']}",
        "",
        f"Apps: {catalog['app_count']}",
        f"Apps with GitHub projects: {catalog['github_app_count']}",
        "",
        "| App | Package ID | Version | License | Categories | Source | Repositories | Summary |",
        "|---|---|---:|---|---|---|---|---|",
    ]
    for app in catalog["apps"]:
        source = app["github_url"] or app["source_url"]
        source_cell = f"[source]({source})" if source else ""
        repo_names = ", ".join(item["name"] for item in app["repositories"])
        summary = app["summary"]
        if len(summary) > 240:
            summary = summary[:237] + "..."
        lines.append(
            "| " + " | ".join(
                markdown_escape(value)
                for value in (
                    app["name"], app["package_id"], app["latest_version"], app["license"],
                    ", ".join(app["categories"]), source_cell, repo_names, summary,
                )
            ) + " |"
        )
    lines.append("")
    return "\n".join(lines)


def render_text(catalog: dict[str, Any]) -> str:
    columns = (
        "name", "package_id", "latest_version", "license", "categories", "source_url",
        "github_url", "repositories", "summary",
    )
    lines = ["\t".join(columns)]
    for app in catalog["apps"]:
        values = {
            **app,
            "categories": ", ".join(app["categories"]),
            "repositories": ", ".join(item["name"] for item in app["repositories"]),
        }
        lines.append(
            "\t".join(
                str(values.get(column, "")).replace("\t", " ").replace("\n", " ")
                for column in columns
            ).rstrip()
        )
    return "\n".join(lines) + "\n"


def parse_repo(value: str) -> tuple[str, str]:
    if "=" in value:
        name, url = value.split("=", 1)
        if name.strip() and url.strip():
            return name.strip(), url.strip()
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return parsed.netloc, url
    raise argparse.ArgumentTypeError("repository must be NAME=URL or an http(s) URL")


def search_catalog(path: Path, query: str) -> int:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"Cannot read {path}: {error}", file=sys.stderr)
        return 2
    words = query.casefold().split()
    matches = []
    for app in catalog.get("apps", []):
        haystack = json.dumps(app, ensure_ascii=False).casefold()
        if all(word in haystack for word in words):
            matches.append(app)
    for app in matches:
        source = app.get("github_url") or app.get("source_url") or "no source URL"
        print(f"{app.get('name')}\t{app.get('package_id')}\t{source}\t{app.get('summary', '')}")
    print(f"\n{len(matches)} match(es)", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and search app metadata from F-Droid-compatible repositories."
    )
    parser.add_argument(
        "--repo", action="append", type=parse_repo, metavar="NAME=URL",
        help="repository to collect; repeatable (replaces built-in defaults)",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--locale", default="en-US", help="preferred metadata locale")
    parser.add_argument("--timeout", type=float, default=60, help="per-request timeout in seconds")
    parser.add_argument("--max-download-mb", type=int, default=150, help="maximum size of each index")
    parser.add_argument("--workers", type=int, default=4, help="parallel repository downloads")
    parser.add_argument("--enrich-github", action="store_true", help="query GitHub API for project metadata")
    parser.add_argument(
        "--github-limit", type=int, default=50,
        help="maximum GitHub projects to query; 0 means all (default: 50)",
    )
    parser.add_argument("--search", metavar="WORDS", help="search the existing catalog without downloading")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    json_path = args.output_dir / "apps.json"
    if args.search is not None:
        return search_catalog(json_path, args.search)
    if args.timeout <= 0 or args.max_download_mb <= 0 or args.workers <= 0 or args.github_limit < 0:
        print("timeout, size, and worker values must be positive; GitHub limit cannot be negative", file=sys.stderr)
        return 2

    repositories = args.repo or list(DEFAULT_REPOS)
    print(f"Collecting {len(repositories)} repositories...", file=sys.stderr)
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers, len(repositories))) as executor:
        futures = {
            executor.submit(
                fetch_repository, name, url, args.locale, args.timeout,
                args.max_download_mb * 1_000_000,
            ): (name, url)
            for name, url in repositories
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            if result["status"] == "ok":
                print(f"  OK {result['name']}: {result['app_count']} apps", file=sys.stderr)
            else:
                print(f"  ERROR {result['name']}: {result['error']}", file=sys.stderr)

    results.sort(key=lambda item: item["name"].casefold())
    records = merge_records(results)
    github_errors: list[str] = []
    enriched_count = 0
    if args.enrich_github:
        print("Enriching GitHub projects...", file=sys.stderr)
        enriched_count, github_errors = enrich_github(records, args.github_limit, args.timeout)

    public_repo_results = [{key: value for key, value in result.items() if key != "records"} for result in results]
    catalog = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "app_count": len(records),
        "github_app_count": sum(bool(record["github_url"]) for record in records),
        "github_projects_enriched": enriched_count,
        "repositories": public_repo_results,
        "github_errors": github_errors,
        "apps": records,
    }
    json_text = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    atomic_write(json_path, json_text)
    atomic_write(args.output_dir / "apps.md", render_markdown(catalog))
    atomic_write(args.output_dir / "apps.txt", render_text(catalog))
    failures = sum(result["status"] != "ok" for result in results)
    print(
        f"Wrote {len(records)} unique apps to {json_path}, {args.output_dir / 'apps.md'}, "
        f"and {args.output_dir / 'apps.txt'} ({catalog['github_app_count']} have GitHub links).",
        file=sys.stderr,
    )
    return 1 if failures == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
