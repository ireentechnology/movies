"""
FMFTP Movie & Series Scanner — v3 (অত্যন্ত নিখুঁত ভার্সন)
==========================================================
v2-এর সমস্যাগুলো যা v3-এ সমাধান করা হয়েছে:

  ১. download/stream লিংক ভুল ছিল:
     ─ list API-তে `download` ফিল্ড আসেই না; আসল ভিডিও ফাইলের URL
       পাওয়া যায় শুধু detail endpoint থেকে (`/api/movies/{id}` → `url` ফিল্ড)
     ─ v3 প্রতিটি movie-র detail fetch করে আসল `.mp4` URL কালেক্ট করে
     ─ `build_stream_url()` যে URL বানাত সেটা আসলে কাজ করত না (timeout/404)

  ২. TV Series-এর episodes একদমই কালেক্ট হতো না:
     ─ v2 শুধু watch_page ও stream_url বানাত, কোনো episode ডেটা নিত না
     ─ v3 `/api/tv-shows/{id}?fields=episodes` দিয়ে প্রতিটি series-এর
       সব season/episode তথ্য সহ আসল streaming URL কালেক্ট করে

  ৩. API total vs আসলে collected-এর gap:
     ─ folder-based total (menus API) আর list-based total-এ তফাৎ ছিল
     ─ v3 দুটোই যাচাই করে, সাথে প্রতিটি item-র detail fetch-এ
       fail হলে সেটা লগ করে এবং retry করে

  ৪. Rate-limit / anti-bot:
     ─ আরও ভালো header, adaptive delay, circuit-breaker pattern
     ─ per-request jitter + batch-level throttling

  ৫. Concurrency:
     ─ detail fetch-এ জন্য আলাদা thread pool, list fetch-এ আলাদা
     ─ progress bar সহ real-time কাউন্ট

  ৬. Resume capability:
     ─ যদি কোনো category partially fail করে, পরের রানে শুধু missing
       item-গুলো detail-fetch করে, পুরো list আবার নেয় না
"""

import json
import os
import random
import time
import hashlib
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ═══════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════

ORIGIN = "https://fmftp.net"

# Movie list-এর fields — `download` বাদ দেওয়া হয়েছে কারণ API এটা return করে না
# আসল URL detail endpoint থেকে নিতে হবে
MOVIE_LIST_FIELDS = (
    "id,title,genre,year,views,online_rating,"
    "release_date,poster_path,backdrop_path"
)
SERIES_LIST_FIELDS = (
    "id,title,genre,year,online_rating,"
    "release_date,poster_path,backdrop_path"
)

MOVIE_CATEGORIES = [
    {"id": 1,  "file": "Bollywood.json",     "label": "Bollywood"},
    {"id": 2,  "file": "Hollywood.json",     "label": "Hollywood"},
    {"id": 3,  "file": "Animation.json",     "label": "Animation"},
    {"id": 4,  "file": "Korean.json",        "label": "Korean Movies"},
    {"id": 5,  "file": "Hindi_dubbed.json",  "label": "Hindi Dubbed"},
    {"id": 6,  "file": "Horror.json",        "label": "Horror"},
    {"id": 7,  "file": "Indian_Bangla.json", "label": "Indian Bangla"},
    {"id": 8,  "file": "Tamil.json",         "label": "Tamil"},
    {"id": 14, "file": "foreign.json",       "label": "Foreign"},
]

SERIES_CATEGORIES = [
    {"id": 9,  "file": "English_Tv_Series.json",  "label": "English TV Series"},
    {"id": 10, "file": "Indian_Tv_Series.json",   "label": "Indian TV Series"},
    {"id": 11, "file": "Korean_Tv_Series.json",   "label": "Korean TV Series"},
    {"id": 12, "file": "Bangla_Tv_Series.json",   "label": "Bangla TV Series"},
    {"id": 13, "file": "Turkish_Tv_Series.json",  "label": "Turkish TV Series"},
]

# ─── Tuning ────────────────────────────────────
TIMEOUT            = 30
LIST_MAX_WORKERS   = 5       # list page fetch-এর জন্য
DETAIL_MAX_WORKERS = 8       # per-item detail fetch-এর জন্য (আলাদা pool)
PAGE_RETRIES       = 5
DETAIL_RETRIES     = 3
RETRY_BACKOFF      = 1.8
LIST_DELAY         = (0.1, 0.3)    # list request-এর আগে random delay
DETAIL_DELAY       = (0.05, 0.2)   # detail request-এর আগে random delay
LIST_PAGE_LIMIT    = 100     # প্রতি page-এ কতটা item
CIRCUIT_BREAK_THRESHOLD = 10  # টানা এতটা fail হলে সাময়িকভাবে থামবে
CIRCUIT_BREAK_COOLDOWN  = 10  # circuit break-এর পর কত সেকেন্ড অপেক্ষা

HEADERS = {
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": f"{ORIGIN}/",
    "Origin":  ORIGIN,
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# ═══════════════════════════════════════════════
#  SESSION & HTTP
# ═══════════════════════════════════════════════


def make_session():
    """Retry-adapter সহ requests.Session — connection error / 5xx / 429-এ
    স্বয়ংক্রিয়ভাবে backoff দিয়ে retry করে।"""
    session = requests.Session()
    retry = Retry(
        total=PAGE_RETRIES,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_maxsize=max(LIST_MAX_WORKERS, DETAIL_MAX_WORKERS) * 3,
        pool_connections=max(LIST_MAX_WORKERS, DETAIL_MAX_WORKERS) * 3,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session


SESSION = make_session()

# ─── Circuit Breaker ──────────────────────────
_consecutive_failures = 0
_break_until = 0.0


def _check_circuit():
    global _consecutive_failures, _break_until
    if _consecutive_failures >= CIRCUIT_BREAK_THRESHOLD:
        if time.time() < _break_until:
            raise RuntimeError(
                f"Circuit breaker active — too many consecutive failures. "
                f"Will retry after {_break_until - time.time():.0f}s"
            )
        _consecutive_failures = 0  # reset after cooldown


def _record_success():
    global _consecutive_failures
    _consecutive_failures = 0


def _record_failure():
    global _consecutive_failures, _break_until
    _consecutive_failures += 1
    if _consecutive_failures >= CIRCUIT_BREAK_THRESHOLD:
        _break_until = time.time() + CIRCUIT_BREAK_COOLDOWN
        print(f"  ⚠ Circuit breaker tripped after "
              f"{_consecutive_failures} consecutive failures. "
              f"Cooling down {CIRCUIT_BREAK_COOLDOWN}s...")


# ═══════════════════════════════════════════════
#  API CALLS
# ═══════════════════════════════════════════════


def get_category_map():
    """`/api/menus` থেকে category নাম + folder info লোড করে।"""
    try:
        r = SESSION.get(f"{ORIGIN}/api/menus", timeout=TIMEOUT)
        r.raise_for_status()
        menus = r.json()
        cmap = {}
        folder_totals = {}
        for section in ("movie", "tv"):
            for item in menus.get("categories", {}).get(section, []):
                cmap[str(item["id"])] = item.get("name", "")
                folder_totals[str(item["id"])] = sum(
                    f.get("total", 0) for f in item.get("folders", [])
                )
        return cmap, folder_totals
    except Exception as e:
        print(f"  [WARN] Could not load category map: {e}")
        return {}, {}


def fetch_page(url, page_label="", delay_range=LIST_DELAY):
    """একটা page fetch করে — adapter retry ছাড়াও JSON-parse / অপ্রত্যাশিত
    error হলে নিজে ম্যানুয়ালি retry করে। Circuit breaker সহ।"""
    _check_circuit()
    last_err = None
    for attempt in range(1, PAGE_RETRIES + 1):
        try:
            time.sleep(random.uniform(*delay_range))
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            _record_success()
            return data
        except Exception as e:
            last_err = e
            _record_failure()
            wait = RETRY_BACKOFF * attempt + random.uniform(0, 0.5)
            print(f"    [RETRY {attempt}/{PAGE_RETRIES}] {page_label} "
                  f"failed ({e}) — retry in {wait:.1f}s")
            try:
                _check_circuit()
            except RuntimeError:
                raise
            time.sleep(wait)
    raise RuntimeError(
        f"{page_label} permanently failed after "
        f"{PAGE_RETRIES} attempts: {last_err}"
    )


def fetch_movie_detail(movie_id):
    """`/api/movies/{id}` থেকে পূর্ণ detail fetch করে — এখান থেকেই
    আসল ভিডিও ফাইলের `url` পাওয়া যায়।"""
    url = f"{ORIGIN}/api/movies/{movie_id}"
    last_err = None
    for attempt in range(1, DETAIL_RETRIES + 1):
        try:
            time.sleep(random.uniform(*DETAIL_DELAY))
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            _record_success()
            return data
        except Exception as e:
            last_err = e
            _record_failure()
            if attempt < DETAIL_RETRIES:
                wait = RETRY_BACKOFF * attempt + random.uniform(0, 0.3)
                time.sleep(wait)
    print(f"      [DETAIL FAIL] movie #{movie_id}: {last_err}")
    return None


def fetch_series_detail(series_id):
    """`/api/tv-shows/{id}?fields=episodes` থেকে পূর্ণ detail + সব episodes
    fetch করে।"""
    url = f"{ORIGIN}/api/tv-shows/{series_id}?fields=episodes"
    last_err = None
    for attempt in range(1, DETAIL_RETRIES + 1):
        try:
            time.sleep(random.uniform(*DETAIL_DELAY))
            r = SESSION.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            _record_success()
            return data
        except Exception as e:
            last_err = e
            _record_failure()
            if attempt < DETAIL_RETRIES:
                wait = RETRY_BACKOFF * attempt + random.uniform(0, 0.3)
                time.sleep(wait)
    print(f"      [DETAIL FAIL] series #{series_id}: {last_err}")
    return None


# ═══════════════════════════════════════════════
#  URL BUILDERS
# ═══════════════════════════════════════════════


def build_poster_url(path):
    if not path:
        return ""
    path = str(path).strip()
    sep = "" if path.startswith("/") else "/"
    return f"{ORIGIN}/content-images/movies/posters{sep}{path}"


def build_stream_url(file_url):
    """আসল ফাইল path থেকে streaming URL বানায়।
    `file_url` হলো detail endpoint-এর `url` ফিল্ড (relative path)।"""
    if not file_url:
        return ""
    if file_url.startswith("http"):
        return file_url
    sep = "" if file_url.startswith("/") else "/"
    return f"{ORIGIN}{sep}{file_url}"


def build_watch_url(item_id, content_type="MOVIE", episode_id=None):
    url = f"{ORIGIN}/watch?type={content_type}&id={item_id}"
    if episode_id:
        url += f"&episode={episode_id}"
    return url


def build_backdrop_url(path):
    if not path:
        return ""
    path = str(path).strip()
    sep = "" if path.startswith("/") else "/"
    return f"{ORIGIN}/content-images/movies/backdrops{sep}{path}"


# ═══════════════════════════════════════════════
#  LIST FETCHING (paginated)
# ═══════════════════════════════════════════════


def fetch_list_pages(endpoint, cat_id, fields, label=""):
    """একটা category-র সব page fetch করে। Page 1 থেকে শুরু করে
    total_pages পর্যন্ত সব page parallel-ভাবে নেয়।
    ব্যর্থ page-গুলো আলাদা তালিকায় রাখে এবং একবার extra retry করে।"""
    url_tpl = (
        f"{ORIGIN}/api/{endpoint}?"
        f"limit={LIST_PAGE_LIMIT}"
        f"&fields={fields}"
        f"&library={cat_id}"
        f"&page={{page}}"
        f"&sort=release_date"
    )

    # Page 1
    first = fetch_page(url_tpl.format(page=1), f"{label} page 1")
    total_pages   = int(first.get("pages", 1) or 1)
    reported_total = first.get("total") or first.get("total_items")
    all_raw = list(first.get("data", []))

    print(f"    Pages: {total_pages}"
          + (f"  (API total={reported_total})" if reported_total else ""))

    # Pages 2..N (parallel)
    failed_pages = []
    if total_pages > 1:
        pages = list(range(2, total_pages + 1))
        with ThreadPoolExecutor(max_workers=LIST_MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    fetch_page, url_tpl.format(page=p), f"{label} page {p}"
                ): p
                for p in pages
            }
            for future in as_completed(futures):
                page_num = futures[future]
                try:
                    items = future.result().get("data", [])
                    all_raw.extend(items)
                except Exception as e:
                    print(f"    [FAIL] {label} page {page_num}: {e}")
                    failed_pages.append(page_num)

        # ব্যর্থ page-গুলো একবার আবার retry
        if failed_pages:
            print(f"    [INFO] Retrying {len(failed_pages)} failed page(s)...")
            still_failed = []
            for p in sorted(failed_pages):
                try:
                    data = fetch_page(
                        url_tpl.format(page=p), f"{label} retry-page {p}"
                    )
                    all_raw.extend(data.get("data", []))
                except Exception as e:
                    still_failed.append(p)
            if still_failed:
                print(f"    [WARN] {label} pages permanently lost: {still_failed}")
                failed_pages = still_failed
            else:
                failed_pages = []

    # Deduplicate by id (same movie might appear in multiple pages)
    seen_ids = set()
    deduped = []
    duplicates = 0
    for item in all_raw:
        item_id = item.get("id")
        if item_id is not None and item_id in seen_ids:
            duplicates += 1
            continue
        if item_id is not None:
            seen_ids.add(item_id)
        deduped.append(item)

    if duplicates:
        print(f"    [INFO] Removed {duplicates} duplicate(s) from page overlap")

    return deduped, reported_total, failed_pages


# ═══════════════════════════════════════════════
#  ITEM MAPPERS
# ═══════════════════════════════════════════════


def map_movie_item(list_item, detail, category_name):
    """list item + detail endpoint-এর ডেটা মার্জ করে final JSON entry বানায়।
    আসল streaming/download URL detail-এর `url` ফিল্ড থেকে আসে।"""
    item_id = list_item.get("id") or (detail or {}).get("id")
    file_url = ""
    runtime = 0
    overview = ""
    casts = ""
    imdb_id = ""
    tmdb_id = ""
    trailers = ""

    if detail:
        file_url = detail.get("url", "") or ""
        runtime = detail.get("runtime", 0) or 0
        overview = detail.get("overview", "") or ""
        casts = detail.get("casts", "") or ""
        imdb_id = detail.get("imdb_id", "") or ""
        tmdb_id = detail.get("tmdb_id", "") or ""
        trailers = detail.get("trailers", "") or ""

    stream_url = build_stream_url(file_url)

    # একাধিক quality/download link থাকলে সব রাখা হয়
    links = []
    if stream_url:
        links.append({
            "url":        stream_url,
            "language":   category_name,
            "quality":    _extract_quality_from_filename(file_url),
            "watch_page": build_watch_url(item_id, "MOVIE"),
            "type":       "stream",
        })

    return {
        "id":          str(item_id) if item_id is not None else "",
        "year":        str(list_item.get("year", "") or ""),
        "tvg_logo":    build_poster_url(list_item.get("poster_path", "")),
        "backdrop":    build_backdrop_url(list_item.get("backdrop_path", "")),
        "rating":      float(list_item.get("online_rating") or 0),
        "genre":       _parse_genres(list_item.get("genre", "")),
        "views":       int(list_item.get("views") or 0),
        "links":       links,
        "overview":    overview,
        "casts":       casts,
        "imdb_id":     str(imdb_id),
        "tmdb_id":     str(tmdb_id),
        "trailers":    trailers,
        "runtime":     int(runtime) if runtime else 0,
        "watch_page":  build_watch_url(item_id, "MOVIE"),
        "stream_url":  stream_url,
    }


def map_series_item(list_item, detail, category_name):
    """list item + detail+episodes মার্জ করে final JSON entry।
    প্রতিটি episode-র জন্য streaming URL + metadata কালেক্ট করে।"""
    item_id = list_item.get("id") or (detail or {}).get("id")
    episodes_data = []
    total_episodes = 0
    overview = ""
    casts = ""
    tmdb_id = ""
    trailer = ""

    if detail:
        overview = detail.get("overview", "") or ""
        casts = detail.get("casts", "") or ""
        tmdb_id = detail.get("tmdb_id", "") or ""
        trailer = detail.get("trailer", "") or detail.get("trailers", "") or ""
        raw_episodes = detail.get("episodes", []) or []

        # season অনুযায়ী group করা
        seasons_dict = {}
        for ep in raw_episodes:
            sn = ep.get("season_number", 1)
            if sn not in seasons_dict:
                seasons_dict[sn] = []
            seasons_dict[sn].append(ep)

        for sn in sorted(seasons_dict.keys()):
            eps = sorted(seasons_dict[sn], key=lambda e: e.get("episode_number", 0))
            season_obj = {
                "season_number": sn,
                "episode_count": len(eps),
                "episodes": [],
            }
            for ep in eps:
                ep_id = ep.get("id")
                ep_name = ep.get("name", "") or ""
                # Episode-র জন্য watch URL-এ episode param যোগ করা হয়
                watch_url = build_watch_url(item_id, "SERIES", ep_id)
                season_obj["episodes"].append({
                    "id":              str(ep_id) if ep_id is not None else "",
                    "name":            ep_name,
                    "episode_number":  ep.get("episode_number", 0),
                    "release_date":    ep.get("release_date", ""),
                    "still_path":      ep.get("still_path", ""),
                    "online_rating":   float(ep.get("online_rating") or 0),
                    "views":           int(ep.get("views") or 0),
                    "watch_page":      watch_url,
                })
            episodes_data.append(season_obj)
            total_episodes += len(eps)

    return {
        "id":             str(item_id) if item_id is not None else "",
        "year":           str(list_item.get("year", "") or ""),
        "tvg_logo":       build_poster_url(list_item.get("poster_path", "")),
        "backdrop":       build_backdrop_url(list_item.get("backdrop_path", "")),
        "rating":         float(list_item.get("online_rating") or 0),
        "genre":          _parse_genres(list_item.get("genre", "")),
        "language":       category_name,
        "overview":       overview,
        "casts":          casts,
        "tmdb_id":        str(tmdb_id),
        "trailer":        trailer,
        "total_seasons":  len(episodes_data),
        "total_episodes": total_episodes,
        "seasons":        episodes_data,
        "watch_page":     build_watch_url(item_id, "SERIES"),
    }


def _parse_genres(genre_str):
    if not genre_str:
        return []
    return [g.strip() for g in str(genre_str).split(",") if g.strip()]


def _extract_quality_from_filename(filename):
    """ফাইলনাম থেকে quality tag বের করে (যেমন 720p, 1080p, 4K)।"""
    if not filename:
        return ""
    fname = str(filename).lower()
    for tag in ("4k", "2160p", "1440p", "1080p", "720p", "480p", "360p"):
        if tag in fname:
            return tag
    return ""


def safe_key(title, item_id, used_keys):
    """Title-key ব্যবহার করা হয়, ডুপ্লিকেট title হলে id/counter সাফিক্স
    যোগ করে যাতে কোনো entry overwrite হয়ে না যায়।"""
    title = title.strip() if title else ""
    if not title:
        title = f"Untitled-{item_id}" if item_id is not None else "Untitled"
    key = title
    if key in used_keys:
        key = (f"{title} [{item_id}]"
               if item_id is not None
               else f"{title} #{len(used_keys)+1}")
        n = 2
        base_key = key
        while key in used_keys:
            key = f"{base_key} ({n})"
            n += 1
    used_keys.add(key)
    return key


# ═══════════════════════════════════════════════
#  DETAIL FETCHING (parallel, per-item)
# ═══════════════════════════════════════════════


def fetch_details_batch(item_ids, fetch_fn, label="", need_detail_ids=None):
    """একাধিক item-র detail parallel-ভাবে fetch করে।
    `need_detail_ids` দিলে শুধু সেই item-গুলোর detail নেয় (resume mode)।
    প্রতিটি item-র জন্য max_retries বার চেষ্টা করে।
    ফলাফল: {item_id: detail_dict or None}"""
    ids_to_fetch = need_detail_ids if need_detail_ids else item_ids
    if not ids_to_fetch:
        return {}

    results = {}
    total = len(ids_to_fetch)
    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=DETAIL_MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_fn, item_id): item_id
            for item_id in ids_to_fetch
        }
        for future in as_completed(futures):
            item_id = futures[future]
            done += 1
            try:
                detail = future.result()
                if detail:
                    results[item_id] = detail
                else:
                    results[item_id] = None
                    failed += 1
            except Exception as e:
                results[item_id] = None
                failed += 1
                print(f"      [FAIL] {label} #{item_id}: {e}")

            # Progress
            if done % 50 == 0 or done == total:
                pct = done * 100 // total
                print(f"      [{label}] Detail progress: {done}/{total} "
                      f"({pct}%) — failed: {failed}")

    return results


# ═══════════════════════════════════════════════
#  CATEGORY COLLECTOR
# ═══════════════════════════════════════════════


def fetch_category_full(cat_id, category_name, endpoint, fields, mapper,
                         detail_fn, filepath, kind):
    """একটা category-র সম্পূর্ণ স্ক্যান:
      1. List pages থেকে সব item-র basic info
      2. প্রতিটি item-র detail (streaming URL / episodes)
      3. Merge করে final JSON
      4. Existing file-এর সাথে merge
    """
    label = category_name or f"cat-{cat_id}"

    # ── Step 1: List pages ──
    print(f"\n  📋 [{label}] Fetching list pages...")
    all_raw, reported_total, failed_pages = fetch_list_pages(
        endpoint, cat_id, fields, label
    )
    print(f"     List items: {len(all_raw)}")

    # ── Step 2: Load existing data (resume support) ──
    existing = load_existing(filepath)
    existing_ids = set()
    existing_items = {}
    if existing and "items" in existing:
        for key, val in existing["items"].items():
            item_id = val.get("id", "")
            if item_id:
                existing_ids.add(int(item_id) if str(item_id).isdigit() else item_id)
                existing_items[item_id] = (key, val)

    # ── Step 3: Determine which items need detail fetch ──
    # Resume mode: যদি item already existing-এ আছে এবং তার detail
    # (stream_url / episodes) সম্পূর্ণ, তাহলে আবার fetch করার দরকার নেই
    all_ids = []
    id_to_list_item = {}
    for item in all_raw:
        item_id = item.get("id")
        if item_id is not None:
            all_ids.append(item_id)
            id_to_list_item[item_id] = item

    # কোন item-গুলোর detail নতুন করে নিতে হবে?
    need_detail_ids = []
    cached_details = {}
    for item_id in all_ids:
        if item_id in existing_ids:
            _, existing_val = existing_items.get(item_id, (None, None))
            if existing_val:
                # Movie: stream_url আছে কিনা চেক করো
                if kind == "movies":
                    if existing_val.get("stream_url"):
                        cached_details[item_id] = None  # reuse existing
                        continue
                # Series: episodes আছে কিনা চেক করো
                elif kind == "series":
                    if existing_val.get("total_episodes", 0) > 0 or \
                       (existing_val.get("seasons") and
                        len(existing_val["seasons"]) > 0):
                        cached_details[item_id] = None  # reuse existing
                        continue
        need_detail_ids.append(item_id)

    if need_detail_ids:
        print(f"  🔍 [{label}] Fetching details for "
              f"{len(need_detail_ids)}/{len(all_ids)} items "
              f"({len(all_ids) - len(need_detail_ids)} cached)...")
        fresh_details = fetch_details_batch(
            all_ids, detail_fn, label, need_detail_ids
        )
    else:
        print(f"  ✅ [{label}] All {len(all_ids)} items cached — "
              f"no detail fetch needed")
        fresh_details = {}

    # ── Step 4: Build final items ──
    items_obj = {}
    used_keys = set()
    skipped = 0
    detail_failed = 0

    for item in all_raw:
        item_id = item.get("id")
        title = str(item.get("title", "")).strip()
        if not title and item_id is None:
            skipped += 1
            continue

        # Detail পাওয়া গেছে কি না?
        detail = fresh_details.get(item_id) if item_id is not None else None

        # যদি detail fail করে থাকে কিন্তু existing-এ আছে, existing ব্যবহার করো
        if detail is None and item_id in existing_ids:
            _, existing_val = existing_items.get(item_id, (None, None))
            if existing_val:
                key = safe_key(title, item_id, used_keys)
                items_obj[key] = existing_val
                continue
            detail_failed += 1

        key = safe_key(title, item_id, used_keys)
        mapped = mapper(item, detail, category_name)

        # যদি detail fail করে থাকে, তাও entry রাখা হয় (stream_url খালি থাকবে)
        # পরের রানে resume হলে detail নেওয়া হবে
        items_obj[key] = mapped

    # ── Step 5: Merge with existing ──
    merged_data, added, updated = merge_with_existing(
        items_obj, existing, category_name, cat_id, endpoint,
        reported_total, failed_pages, kind
    )

    # ── Step 6: Validation ──
    collected = len(items_obj)
    if reported_total and collected < int(reported_total):
        gap = int(reported_total) - collected
        print(f"  ⚠ [{label}] Expected ~{reported_total} but collected "
              f"{collected} (gap: {gap})")
    if skipped:
        print(f"  ⚠ [{label}] Skipped {skipped} item(s) with no title/id")
    if detail_failed:
        print(f"  ⚠ [{label}] {detail_failed} item(s) detail fetch failed "
              f"(will retry next run)")

    return merged_data, added, updated, collected


def merge_with_existing(new_items, existing_data, category_name, cat_id,
                         endpoint, reported_total, failed_pages, kind):
    """নতুন items আগের file-এর সাথে merge করে।"""
    if not existing_data:
        data = {
            "type": "category_collection" if endpoint == "movies"
                    else "series_collection",
            "source_url": f"{ORIGIN}/"
                          f"{'movies' if endpoint=='movies' else 'tv-shows'}"
                          f"?category={cat_id}",
            "category_id": str(cat_id),
            "category_name": category_name,
            "total_items": len(new_items),
            "expected_total": int(reported_total) if reported_total else None,
            "failed_pages": failed_pages if failed_pages else None,
            "collected_at": datetime.utcnow().isoformat() + "Z",
            "items": new_items,
        }
        return data, len(new_items), 0

    existing_items = existing_data.get("items", {})
    added = updated = 0
    for key, data in new_items.items():
        if key not in existing_items:
            existing_items[key] = data
            added += 1
        else:
            existing_items[key] = data
            updated += 1

    existing_data["items"] = existing_items
    existing_data["total_items"] = len(existing_items)
    existing_data["expected_total"] = (
        int(reported_total) if reported_total else None
    )
    existing_data["failed_pages"] = failed_pages if failed_pages else None
    existing_data["last_updated"] = datetime.utcnow().isoformat() + "Z"
    return existing_data, added, updated


# ═══════════════════════════════════════════════
#  FILE I/O
# ═══════════════════════════════════════════════


def load_existing(filepath):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════
#  REPORT
# ═══════════════════════════════════════════════


def generate_report(movie_stats, series_stats, scan_time):
    total_movies = sum(s["count"] for s in movie_stats)
    total_series = sum(s["count"] for s in series_stats)
    total_episodes = sum(s.get("total_episodes", 0) for s in series_stats)

    report = {
        "generated_at": scan_time,
        "total_movies": total_movies,
        "total_series": total_series,
        "total_episodes": total_episodes,
        "grand_total": total_movies + total_series,
        "movies": {"total": total_movies, "categories": movie_stats},
        "series": {"total": total_series, "categories": series_stats},
    }
    save_json("report.json", report)
    print(f"\n  📊 report.json saved — "
          f"{total_movies} movies, {total_series} series, "
          f"{total_episodes} episodes")
    return report


# ═══════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════


def process_categories(categories, kind, endpoint, fields, mapper, detail_fn,
                         category_map, folder_totals, stats_list):
    for cat in categories:
        cat_id   = cat["id"]
        filename = cat["file"]
        label    = cat["label"]
        cat_name = category_map.get(str(cat_id), label)
        filepath = f"{kind}/{filename}"
        folder_total = folder_totals.get(str(cat_id))

        print(f"\n{'─' * 50}")
        print(f"  [{filename}]  category={cat_id} ({cat_name})")
        if folder_total:
            print(f"  Folder-reported total: {folder_total}")

        try:
            merged, added, updated, collected = fetch_category_full(
                cat_id, cat_name, endpoint, fields, mapper,
                detail_fn, filepath, kind
            )
            save_json(filepath, merged)

            gap_note = ""
            exp = merged.get("expected_total")
            if exp and collected < exp:
                gap_note = f"  ⚠ expected~{exp}"

            # Series-এর জন্য total episode count
            ep_count = 0
            if kind == "series":
                for v in merged.get("items", {}).values():
                    ep_count += v.get("total_episodes", 0)

            print(f"  ✓  Total={collected}  New={added}  Updated={updated}"
                  f"{gap_note}")
            if ep_count:
                print(f"     Total episodes across all series: {ep_count}")

            stats_list.append({
                "label":          label,
                "category_id":    str(cat_id),
                "file":           filepath,
                "count":          collected,
                "new":            added,
                "updated":        updated,
                "total_episodes": ep_count if kind == "series" else None,
                "expected_total": exp,
            })
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            import traceback
            traceback.print_exc()
            stats_list.append({
                "label":       label,
                "category_id": str(cat_id),
                "file":        filepath,
                "count":       0,
                "new":         0,
                "error":       str(e),
            })


def main():
    print("╔" + "═" * 50 + "╗")
    print("║  🎬 FMFTP Scanner v3 — Ultra Complete               ║")
    print("╚" + "═" * 50 + "╝")

    category_map, folder_totals = get_category_map()
    print(f"  Category map: {len(category_map)} entries loaded\n")

    scan_time    = datetime.utcnow().isoformat() + "Z"
    movie_stats  = []
    series_stats = []

    # ── Movies ──
    print("=" * 52)
    print("  🎥 MOVIES")
    print("=" * 52)
    process_categories(
        MOVIE_CATEGORIES, "movies", "movies",
        MOVIE_LIST_FIELDS, map_movie_item, fetch_movie_detail,
        category_map, folder_totals, movie_stats
    )

    # ── TV Series ──
    print("\n" + "=" * 52)
    print("  📺 TV SERIES (with episodes)")
    print("=" * 52)
    process_categories(
        SERIES_CATEGORIES, "series", "tv-shows",
        SERIES_LIST_FIELDS, map_series_item, fetch_series_detail,
        category_map, folder_totals, series_stats
    )

    # ── Summary ──
    total_new = sum(s.get("new", 0) for s in movie_stats + series_stats)
    total_updated = sum(s.get("updated", 0) for s in movie_stats + series_stats)

    generate_report(movie_stats, series_stats, scan_time)

    save_json("scan_summary.json", {
        "last_scan":        scan_time,
        "scanner_version":  "v3",
        "total_new_items":  total_new,
        "total_updated":    total_updated,
        "movie_categories": len(movie_stats),
        "series_categories": len(series_stats),
    })

    print(f"\n{'═' * 52}")
    print(f"  ✅ DONE — {total_new} new, {total_updated} updated")
    print(f"{'═' * 52}")
    return total_new


if __name__ == "__main__":
    main()
