#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "series"
OUTPUT_DIR = ROOT / "output_series"

FMFTP_API_BASE = "https://fmftp.net/api"
TV_SHOW_DETAIL_API = FMFTP_API_BASE + "/tv-shows/{show_id}"
EPISODE_STREAM_API = FMFTP_API_BASE + "/stream/video/stream?type=tv_shows&id={episode_id}"

# legacy = top-level { "Title": { ... } }
# wrapped = keep source wrapper + items
OUTPUT_MODE = os.getenv("OUTPUT_MODE", "legacy").strip().lower()

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN_REQUESTS = 0.08


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": "live-tv-channels-series-builder/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
    )
    return session


SESSION = make_session()


def safe_json_load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def safe_json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_language(value: Optional[str], fallback: str = "") -> str:
    if value:
        return value.strip()

    text = fallback.strip()
    if not text:
        return "Unknown"

    text_lower = text.lower()
    text_lower = text_lower.replace("tv series", "").replace("series", "").strip()

    mapping = {
        "bangla": "Bangla",
        "english": "English",
        "indian": "Indian",
        "korean": "Korean",
        "turkish": "Turkish",
    }
    for k, v in mapping.items():
        if k in text_lower:
            return v

    return text.title()


def extract_show_id(item: Dict[str, Any]) -> Optional[int]:
    # Preferred source: watch_page
    watch_page = str(item.get("watch_page", "")).strip()
    if watch_page:
        parsed = urlparse(watch_page)
        qs = parse_qs(parsed.query)
        ids = qs.get("id")
        if ids and ids[0].isdigit():
            return int(ids[0])

    # Fallback: source stream_url sometimes stores show id
    stream_url = str(item.get("stream_url", "")).strip()
    if stream_url:
        parsed = urlparse(stream_url)
        qs = parse_qs(parsed.query)
        ids = qs.get("id")
        if ids and ids[0].isdigit():
            return int(ids[0])

    return None


def fetch_show_details(show_id: int) -> Dict[str, Any]:
    url = TV_SHOW_DETAIL_API.format(show_id=show_id)
    resp = SESSION.get(
        url,
        params={"fields": "episodes"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def sort_and_clean_episodes(episodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    seen_ids = set()

    for ep in episodes or []:
        ep_id = ep.get("id")
        if ep_id is None:
            continue
        if ep_id in seen_ids:
            continue
        seen_ids.add(ep_id)
        cleaned.append(ep)

    cleaned.sort(
        key=lambda x: (
            int(x.get("season_number") or 0),
            int(x.get("episode_number") or 0),
            int(x.get("id") or 0),
        )
    )
    return cleaned


def build_episode_title(series_title: str, ep: Dict[str, Any]) -> str:
    season = int(ep.get("season_number") or 1)
    episode = int(ep.get("episode_number") or 1)
    raw_name = str(ep.get("name") or ep.get("title") or "").strip()

    generic_names = {
        "",
        "episode",
        f"episode {episode}",
        f"ep {episode}",
    }

    if raw_name.lower() in generic_names:
        return f"{series_title} S{season}E{episode}"

    return f"{series_title} S{season}E{episode} - {raw_name}"


def episode_stream_url(episode_id: int) -> str:
    return EPISODE_STREAM_API.format(episode_id=episode_id)


def validate_stream_url(episode_id: int) -> bool:
    """
    Fallback validator for +1/+2 probing.
    Valid if the endpoint is not HTML error page.
    """
    url = episode_stream_url(episode_id)
    try:
        resp = SESSION.get(url, allow_redirects=False, timeout=REQUEST_TIMEOUT)
        ct = (resp.headers.get("content-type") or "").lower()

        if resp.status_code in {200, 206, 301, 302, 303, 307, 308} and "text/html" not in ct:
            return True
    except Exception:
        return False
    return False


def guess_sequential_episode_ids(first_episode_id: int, expected_count: int) -> List[int]:
    """
    Fallback only if exact episode list API fails.
    User's requested heuristic: next episode may be +1 or +2.
    """
    if expected_count <= 0:
        return [first_episode_id]

    found = [first_episode_id]
    current = first_episode_id

    while len(found) < expected_count:
        next_id = None

        # Prefer +1 / +2 first, then small forward scan
        for delta in [1, 2, 3, 4, 5]:
            candidate = current + delta
            if candidate in found:
                continue
            if validate_stream_url(candidate):
                next_id = candidate
                break

        if next_id is None:
            break

        found.append(next_id)
        current = next_id

    return found


def make_links_from_api(
    series_title: str,
    details: Dict[str, Any],
    language: str,
) -> List[Dict[str, Any]]:
    episodes = sort_and_clean_episodes(details.get("episodes") or [])
    added = utc_today_str()

    if episodes:
        links = []
        for ep in episodes:
            ep_id = int(ep["id"])
            season = int(ep.get("season_number") or 1)
            episode = int(ep.get("episode_number") or 1)
            links.append(
                {
                    "added": added,
                    "language": language,
                    "season": season,
                    "episode": episode,
                    "episode_title": build_episode_title(series_title, ep),
                    "url": episode_stream_url(ep_id),
                }
            )
        return links

    # Fallback mode if exact episode API ever breaks
    first_episode_id = details.get("first_episode_id")
    expected_count = int(details.get("number_of_episodes") or details.get("total_episodes") or 0)

    if not first_episode_id:
        return []

    guessed_ids = guess_sequential_episode_ids(int(first_episode_id), expected_count or 1)
    links = []

    for idx, ep_id in enumerate(guessed_ids, start=1):
        links.append(
            {
                "added": added,
                "language": language,
                "season": 1,
                "episode": idx,
                "episode_title": f"{series_title} S1E{idx}",
                "url": episode_stream_url(ep_id),
            }
        )

    return links


def build_legacy_output(
    source_data: Dict[str, Any],
    source_name: str,
) -> Dict[str, Any]:
    source_items = source_data.get("items", {})
    category_name = str(source_data.get("category_name", "")).strip()
    out: Dict[str, Any] = {}

    for idx, (series_title, item) in enumerate(source_items.items(), start=1):
        show_id = extract_show_id(item)
        entry = deepcopy(item)

        if not show_id:
            entry["links"] = []
            entry["_error"] = "show_id_not_found"
            out[series_title] = entry
            continue

        try:
            details = fetch_show_details(show_id)
            language = normalize_language(entry.get("language"), category_name)
            links = make_links_from_api(series_title, details, language)

            entry["links"] = links
            entry["episode_count"] = len(links)
            entry["series_id"] = show_id
            out[series_title] = entry
        except Exception as e:
            entry["links"] = []
            entry["_error"] = f"{type(e).__name__}: {e}"
            entry["series_id"] = show_id
            out[series_title] = entry

        if idx % 20 == 0:
            print(f"[{source_name}] processed {idx}/{len(source_items)}")
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    return out


def build_wrapped_output(
    source_data: Dict[str, Any],
    source_name: str,
) -> Dict[str, Any]:
    result = deepcopy(source_data)
    source_items = source_data.get("items", {})
    category_name = str(source_data.get("category_name", "")).strip()
    new_items: Dict[str, Any] = {}

    for idx, (series_title, item) in enumerate(source_items.items(), start=1):
        show_id = extract_show_id(item)
        entry = deepcopy(item)

        if not show_id:
            entry["links"] = []
            entry["_error"] = "show_id_not_found"
            new_items[series_title] = entry
            continue

        try:
            details = fetch_show_details(show_id)
            language = normalize_language(entry.get("language"), category_name)
            links = make_links_from_api(series_title, details, language)

            entry["links"] = links
            entry["episode_count"] = len(links)
            entry["series_id"] = show_id
            new_items[series_title] = entry
        except Exception as e:
            entry["links"] = []
            entry["_error"] = f"{type(e).__name__}: {e}"
            entry["series_id"] = show_id
            new_items[series_title] = entry

        if idx % 20 == 0:
            print(f"[{source_name}] processed {idx}/{len(source_items)}")
        time.sleep(SLEEP_BETWEEN_REQUESTS)

    result["items"] = new_items
    result["built_at"] = utc_now_iso()
    result["builder"] = "build_series_output.py"
    return result


def iter_source_files() -> List[Path]:
    files = sorted(
        p for p in SOURCE_DIR.glob("*_Tv_Series.json")
        if p.is_file()
    )
    return files


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files = iter_source_files()
    if not files:
        raise SystemExit("No *_Tv_Series.json files found in /series")

    print(f"Found {len(files)} source files")

    for file_path in files:
        print(f"Processing: {file_path.name}")
        source_data = safe_json_load(file_path)

        if OUTPUT_MODE == "wrapped":
            output_data = build_wrapped_output(source_data, file_path.name)
        else:
            output_data = build_legacy_output(source_data, file_path.name)

        out_path = OUTPUT_DIR / file_path.name
        safe_json_dump(out_path, output_data)
        print(f"Written: {out_path}")

    print("Done.")


if __name__ == "__main__":
    main()
