#!/usr/bin/env python3
"""
FMFTP TV series scraper.

Builds output JSON files in the same top-level shape as the provided
series.json sample:

{
  "Series Title": {
    "year": "2026",
    "tvg_logo": "https://...jpg",
    "links": [
      {
        "added": "2026-06-24",
        "language": "English",
        "season": 1,
        "episode": 1,
        "episode_title": "Series Title S01E01",
        "url": "https://fmftp.net/data/...mp4"
      }
    ]
  }
}

Supported outputs:
- English_Tv_Series.json
- Hindi_Tv_Series.json
- Dubbed_Tv_Series.json
- Bangla_series.json
- korian_series.json

Notes:
- Korean data source: https://fmftp.net/tv-shows?category=11
- Bangla data source:  https://fmftp.net/tv-shows?category=12
- Data is collected from the public FMFTP API.
- "Dubbed" detection is heuristic because the public API does not expose
  a dedicated dubbed category.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://fmftp.net/api"
LIST_API = f"{API_BASE}/tv-shows"
DETAIL_API = f"{API_BASE}/tv-shows/{{show_id}}"
STREAM_API = f"{API_BASE}/stream/video/stream?type=tv_shows&id={{episode_id}}"
TV_POSTER_BASE = "https://fmftp.net/content-images/tv-shows/posters"
TV_BACKDROP_BASE = "https://fmftp.net/content-images/tv-shows/backdrops"
TV_LOGO_BASE = "https://fmftp.net/content-images/tv-shows/logos"

REQUEST_TIMEOUT = 30
PAGE_SIZE = 100
DEFAULT_WORKERS = 6
DEFAULT_DELAY = 0.05
DEFAULT_OUTPUT_DIR = os.path.join("common", "log", "SE")
MAX_PAGES = 0  # 0 = all pages

VALID_MEDIA_CONTENT_TYPES = (
    "video/",
    "audio/",
    "application/octet-stream",
    "application/x-matroska",
)
VALID_MEDIA_EXTENSIONS = (
    ".mp4",
    ".mkv",
    ".avi",
    ".webm",
    ".m4v",
    ".ts",
    ".mov",
)
GENERIC_EPISODE_NAMES = {"", "episode"}
DUBBED_REGEX = re.compile(
    r"dubbed|dual|multi(?:\s|[-_])?audio|hindi|bangla|bengali|tamil|telugu|malayalam|korean",
    re.IGNORECASE,
)
SXXEXX_REGEX = re.compile(r"s(\d{1,2})e(\d{1,3})", re.IGNORECASE)

CATEGORIES = {
    "English_Tv_Series": {
        "library_ids": [9],
        "language": "English",
        "output_file": "English_Tv_Series.json",
        "title_filter": None,
    },
    "Hindi_Tv_Series": {
        "library_ids": [10],
        "language": "Hindi",
        "output_file": "Hindi_Tv_Series.json",
        "title_filter": None,
    },
    "Dubbed_Tv_Series": {
        "library_ids": [9, 10],
        "language": "Multi Audio",
        "output_file": "Dubbed_Tv_Series.json",
        "title_filter": DUBBED_REGEX,
    },
    "Bangla_series": {
        "library_ids": [12],
        "language": "Bangla",
        "output_file": "Bangla_series.json",
        "title_filter": None,
    },
    "korian_series": {
        "library_ids": [11],
        "language": "Korean",
        "output_file": "korian_series.json",
        "title_filter": None,
    },
}

log = logging.getLogger("fmftp_series_scraper")


@dataclass
class ValidationResult:
    ok: bool
    final_url: str = ""
    status_code: int = 0
    content_type: str = ""
    content_length: str = ""
    note: str = ""


@dataclass
class ScanStats:
    categories: int = 0
    series_seen: int = 0
    series_written: int = 0
    links_seen: int = 0
    links_valid: int = 0
    links_invalid: int = 0
    invalid_samples: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)

    def bump(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, getattr(self, key) + value)

    def add_invalid_sample(self, sample: dict, max_items: int = 200) -> None:
        with self.lock:
            if len(self.invalid_samples) < max_items:
                self.invalid_samples.append(sample)

    def add_changed_file(self, path: str) -> None:
        with self.lock:
            self.changed_files.append(path)


def clean(value: str = "") -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str | None) -> str:
    text = clean(value or "")
    if not text:
        return today()
    return text[:10]


def build_image_url(path: str | None, kind: str = "poster") -> str:
    path = clean(path or "")
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path

    base_map = {
        "poster": TV_POSTER_BASE,
        "backdrop": TV_BACKDROP_BASE,
        "logo": TV_LOGO_BASE,
    }
    base = base_map.get(kind, TV_POSTER_BASE)
    return f"{base}{'' if path.startswith('/') else '/'}{path}"


def build_stream_api_url(episode_id: int) -> str:
    return STREAM_API.format(episode_id=episode_id)


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=40)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "FMFTP-Series-Scanner/3.0",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def http_json(session: requests.Session, url: str, **kwargs) -> dict | None:
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        log.warning("Request failed for %s: %s", url, exc)
        return None


def fetch_series_page(session: requests.Session, library_id: int, page: int) -> dict:
    data = http_json(
        session,
        LIST_API,
        params={
            "library": library_id,
            "limit": PAGE_SIZE,
            "page": page,
            "sort": "release_date",
            "fields": "id,title,original_title,year,release_date,logo_path,poster_path,backdrop_path,path,url,languages,genre,updatedAt,createdAt",
        },
    )
    return data or {"data": [], "pages": 0, "total": 0}


def fetch_all_series_for_library(session: requests.Session, library_id: int, workers: int) -> list[dict]:
    first = fetch_series_page(session, library_id, 1)
    total_pages = int(first.get("pages") or 1)
    if MAX_PAGES > 0:
        total_pages = min(total_pages, MAX_PAGES)
    items = list(first.get("data") or [])
    if total_pages <= 1:
        return items

    page_numbers = list(range(2, total_pages + 1))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(fetch_series_page, session, library_id, page_num): page_num
            for page_num in page_numbers
        }
        for future in as_completed(futures):
            page_num = futures[future]
            try:
                payload = future.result()
                items.extend(payload.get("data") or [])
            except Exception as exc:  # pragma: no cover
                log.warning("library %s page %s failed: %s", library_id, page_num, exc)
    return items


def fetch_show_episodes(session: requests.Session, show_id: int) -> list[dict]:
    data = http_json(session, DETAIL_API.format(show_id=show_id), params={"fields": "episodes"})
    if not data:
        return []
    episodes = list(data.get("episodes") or [])
    episodes.sort(
        key=lambda item: (
            int(item.get("season_number") or 0),
            int(item.get("episode_number") or 0),
            int(item.get("id") or 0),
        )
    )
    return episodes


def should_keep_for_dubbed(item: dict) -> bool:
    haystacks = [
        item.get("title"),
        item.get("original_title"),
        item.get("path"),
        item.get("url"),
        item.get("languages"),
        item.get("genre"),
    ]
    merged = " | ".join(clean(value) for value in haystacks if value)
    return bool(DUBBED_REGEX.search(merged))


def collect_category_items(name: str, config: dict, session: requests.Session, workers: int) -> list[dict]:
    unique: dict[int, dict] = {}
    for library_id in config["library_ids"]:
        log.info("[%s] fetching library=%s", name, library_id)
        for item in fetch_all_series_for_library(session, library_id, workers):
            show_id = item.get("id")
            if show_id is None:
                continue
            title = clean(item.get("title"))
            if not title:
                continue
            if name == "Dubbed_Tv_Series" and not should_keep_for_dubbed(item):
                continue
            unique[int(show_id)] = item
    items = list(unique.values())
    items.sort(key=lambda entry: clean(entry.get("title", "")).lower())
    return items


def build_episode_title(series_title: str, episode: dict) -> str:
    season = int(episode.get("season_number") or 1)
    number = int(episode.get("episode_number") or 1)
    raw_name = clean(episode.get("name") or episode.get("title") or "")
    lowered = raw_name.lower()
    if lowered in GENERIC_EPISODE_NAMES or lowered == f"episode {number}":
        return f"{series_title} S{season}E{number}"
    return f"{series_title} S{season}E{number} - {raw_name}"


def make_absolute_url(location: str) -> str:
    if not location:
        return ""
    if location.startswith("//"):
        return f"https:{location}"
    if location.startswith("http://") or location.startswith("https://"):
        return location
    return urljoin("https://fmftp.net", location)


def is_valid_content_type(content_type: str) -> bool:
    lowered = (content_type or "").lower()
    return any(lowered.startswith(prefix) for prefix in VALID_MEDIA_CONTENT_TYPES)


def resolve_direct_media_url(session: requests.Session, episode_id: int) -> ValidationResult:
    api_url = build_stream_api_url(episode_id)
    try:
        response = session.get(api_url, timeout=REQUEST_TIMEOUT, allow_redirects=False, stream=True)
        status = response.status_code
        content_type = response.headers.get("Content-Type", "")
        if status in {301, 302, 303, 307, 308}:
            location = make_absolute_url(response.headers.get("Location", ""))
            response.close()
            if location:
                return ValidationResult(ok=True, final_url=location, status_code=status, content_type=content_type)
            return ValidationResult(ok=False, status_code=status, content_type=content_type, note="redirect-without-location")
        if status in {200, 206} and is_valid_content_type(content_type):
            final_url = response.url
            response.close()
            return ValidationResult(ok=True, final_url=final_url, status_code=status, content_type=content_type)
        preview_note = f"unexpected-stream-status:{status}"
        response.close()
        return ValidationResult(ok=False, status_code=status, content_type=content_type, note=preview_note)
    except requests.RequestException as exc:
        return ValidationResult(ok=False, note=f"stream-resolve-error:{exc.__class__.__name__}")


def validate_media_url_advanced(
    session: requests.Session,
    direct_url: str,
    season: int,
    episode: int,
) -> ValidationResult:
    if not direct_url:
        return ValidationResult(ok=False, note="empty-direct-url")

    last_error = ""
    methods = ["HEAD", "GET"]
    for method in methods:
        try:
            kwargs = {
                "timeout": REQUEST_TIMEOUT,
                "allow_redirects": True,
                "stream": True,
            }
            headers = {}
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            response = session.request(method, direct_url, headers=headers, **kwargs)
            status = response.status_code
            content_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length") or response.headers.get("Content-Range", "")
            final_url = response.url

            if status not in {200, 206}:
                last_error = f"probe-status:{status}"
                response.close()
                continue

            path = urlparse(final_url).path.lower()
            ext_ok = any(path.endswith(ext) for ext in VALID_MEDIA_EXTENSIONS)
            ct_ok = is_valid_content_type(content_type)
            if not (ct_ok or ext_ok):
                last_error = f"bad-content-type:{content_type or 'unknown'}"
                response.close()
                continue

            note = ""
            basename = os.path.basename(path)
            match = SXXEXX_REGEX.search(basename)
            if match:
                found_season = int(match.group(1))
                found_episode = int(match.group(2))
                if found_season != season or found_episode != episode:
                    note = f"episode-mismatch:{found_season}x{found_episode}"

            response.close()
            return ValidationResult(
                ok=True,
                final_url=final_url,
                status_code=status,
                content_type=content_type,
                content_length=content_length,
                note=note,
            )
        except requests.RequestException as exc:
            last_error = f"probe-error:{exc.__class__.__name__}"

    return ValidationResult(ok=False, final_url=direct_url, note=last_error or "advanced-validation-failed")


def build_episode_link(
    session: requests.Session,
    series_title: str,
    category_language: str,
    episode: dict,
    validate_links: str,
    drop_invalid_links: bool,
    stats: ScanStats,
) -> dict | None:
    season = int(episode.get("season_number") or 1)
    number = int(episode.get("episode_number") or 1)
    episode_id = episode.get("id")
    added = parse_date(episode.get("updatedAt") or episode.get("createdAt") or episode.get("release_date"))

    stats.bump(links_seen=1)

    if episode_id is None:
        stats.bump(links_invalid=1)
        stats.add_invalid_sample(
            {
                "series": series_title,
                "season": season,
                "episode": number,
                "reason": "missing-episode-id",
            }
        )
        return None if drop_invalid_links else {
            "added": added,
            "language": category_language,
            "season": season,
            "episode": number,
            "episode_title": build_episode_title(series_title, episode),
            "url": "",
        }

    resolved = resolve_direct_media_url(session, int(episode_id))
    if not resolved.ok or not resolved.final_url:
        stats.bump(links_invalid=1)
        stats.add_invalid_sample(
            {
                "series": series_title,
                "season": season,
                "episode": number,
                "episode_id": episode_id,
                "reason": resolved.note or "resolve-failed",
            }
        )
        if drop_invalid_links:
            return None
        return {
            "added": added,
            "language": category_language,
            "season": season,
            "episode": number,
            "episode_title": build_episode_title(series_title, episode),
            "url": build_stream_api_url(int(episode_id)),
        }

    final_url = resolved.final_url
    if validate_links == "advanced":
        verified = validate_media_url_advanced(session, final_url, season, number)
        if verified.ok and verified.final_url:
            final_url = verified.final_url
            stats.bump(links_valid=1)
        else:
            stats.bump(links_invalid=1)
            stats.add_invalid_sample(
                {
                    "series": series_title,
                    "season": season,
                    "episode": number,
                    "episode_id": episode_id,
                    "reason": verified.note or "advanced-validation-failed",
                    "url": final_url,
                }
            )
            if drop_invalid_links:
                return None
    else:
        stats.bump(links_valid=1)

    return {
        "added": added,
        "language": category_language,
        "season": season,
        "episode": number,
        "episode_title": build_episode_title(series_title, episode),
        "url": final_url,
    }


def build_series_entry(
    session: requests.Session,
    item: dict,
    category_language: str,
    validate_links: str,
    drop_invalid_links: bool,
    delay: float,
    stats: ScanStats,
) -> tuple[str, dict] | None:
    show_id = item.get("id")
    title = clean(item.get("title"))
    if show_id is None or not title:
        return None

    time.sleep(delay)
    episodes = fetch_show_episodes(session, int(show_id))
    poster = (
        build_image_url(item.get("poster_path"), "poster")
        or build_image_url(item.get("logo_path"), "logo")
        or build_image_url(item.get("backdrop_path"), "backdrop")
    )
    year = str(item.get("year") or "")

    links: list[dict] = []
    for episode in episodes:
        link = build_episode_link(
            session=session,
            series_title=title,
            category_language=category_language,
            episode=episode,
            validate_links=validate_links,
            drop_invalid_links=drop_invalid_links,
            stats=stats,
        )
        if link and link.get("url"):
            links.append(link)

    links.sort(key=lambda row: (int(row["season"]), int(row["episode"]), row["url"]))

    return (
        title,
        {
            "year": year,
            "tvg_logo": poster,
            "links": links,
        },
    )


def load_existing_output(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log.warning("Could not read existing file %s: %s", path, exc)
        return {}


def merge_outputs(existing: dict, fresh_entries: dict) -> dict:
    merged: dict[str, dict] = {}

    for title, payload in existing.items():
        if isinstance(payload, dict):
            merged[title] = {
                "year": str(payload.get("year") or ""),
                "tvg_logo": payload.get("tvg_logo") or "",
                "links": list(payload.get("links") or []),
            }

    for title, payload in fresh_entries.items():
        bucket = merged.setdefault(
            title,
            {
                "year": str(payload.get("year") or ""),
                "tvg_logo": payload.get("tvg_logo") or "",
                "links": [],
            },
        )
        if payload.get("year"):
            bucket["year"] = str(payload["year"])
        if payload.get("tvg_logo"):
            bucket["tvg_logo"] = payload["tvg_logo"]

        dedupe = {(int(link.get("season") or 0), int(link.get("episode") or 0), link.get("url") or "") for link in bucket["links"]}
        for link in payload.get("links", []):
            key = (int(link.get("season") or 0), int(link.get("episode") or 0), link.get("url") or "")
            if key in dedupe:
                continue
            dedupe.add(key)
            bucket["links"].append(link)

        bucket["links"].sort(key=lambda row: (int(row["season"]), int(row["episode"]), row["url"]))

    return dict(sorted(merged.items(), key=lambda item: clean(item[0]).lower()))


def atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FMFTP TV series scraper")
    parser.add_argument("--category", choices=list(CATEGORIES.keys()), action="append", help="Run only selected categories")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for output JSON files",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent show workers")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help="Delay between detail scans")
    parser.add_argument(
        "--validate-links",
        choices=["off", "advanced"],
        default=os.environ.get("CF_VALIDATE_LINKS", "advanced"),
        help="Whether to validate resolved episode links",
    )
    parser.add_argument(
        "--drop-invalid-links",
        action="store_true",
        default=os.environ.get("CF_DROP_INVALID_LINKS", "0") == "1",
        help="Skip invalid episode links instead of writing them",
    )
    parser.add_argument(
        "--summary-path",
        default=os.environ.get("CF_SUMMARY_PATH", ""),
        help="Optional summary JSON path",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def run_category(
    name: str,
    config: dict,
    session: requests.Session,
    args: argparse.Namespace,
    overall: ScanStats,
) -> None:
    log.info("=== Category: %s ===", name)
    items = collect_category_items(name, config, session, args.workers)
    overall.bump(categories=1, series_seen=len(items))

    fresh_entries: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                build_series_entry,
                session,
                item,
                config["language"],
                args.validate_links,
                args.drop_invalid_links,
                args.delay,
                overall,
            ): item
            for item in items
        }
        for future in as_completed(futures):
            item = futures[future]
            title = clean(item.get("title"))
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover
                log.warning("[%s] failed to process %s: %s", name, title, exc)
                continue
            if not result:
                continue
            entry_title, payload = result
            fresh_entries[entry_title] = payload

    output_path = os.path.join(args.output_dir, config["output_file"])
    existing = load_existing_output(output_path)
    merged = merge_outputs(existing, fresh_entries)
    atomic_write_json(output_path, merged)
    overall.add_changed_file(output_path)
    overall.bump(series_written=len(merged))
    log.info("[%s] wrote %s (%d series)", name, output_path, len(merged))


def write_summary(path: str, stats: ScanStats, args: argparse.Namespace) -> None:
    if not path:
        return
    payload = {
        "generated_at": utc_now_iso(),
        "validate_links": args.validate_links,
        "drop_invalid_links": args.drop_invalid_links,
        "categories": stats.categories,
        "series_seen": stats.series_seen,
        "series_written": stats.series_written,
        "links_seen": stats.links_seen,
        "links_valid": stats.links_valid,
        "links_invalid": stats.links_invalid,
        "changed_files": stats.changed_files,
        "invalid_samples": stats.invalid_samples,
    }
    atomic_write_json(path, payload)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    session = build_session()
    stats = ScanStats()
    selected = args.category or list(CATEGORIES.keys())

    for category_name in selected:
        run_category(category_name, CATEGORIES[category_name], session, args, stats)

    write_summary(args.summary_path, stats, args)
    log.info(
        "DONE: categories=%d series=%d links=%d valid=%d invalid=%d",
        stats.categories,
        stats.series_written,
        stats.links_seen,
        stats.links_valid,
        stats.links_invalid,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
