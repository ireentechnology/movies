#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output_series"
INDEX_FILE = OUTPUT_DIR / "_index.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def count_links(entry: Dict[str, Any]) -> int:
    links = entry.get("links")
    return len(links) if isinstance(links, list) else 0


def summarize_file(path: Path) -> Dict[str, Any]:
    data = load_json(path)

    if "items" in data and isinstance(data["items"], dict):
        items = data["items"]
        mode = "wrapped"
    else:
        items = data
        mode = "legacy"

    total_series = 0
    total_episodes = 0
    series_with_episodes = 0
    series_with_errors = 0

    for _, entry in items.items():
        if not isinstance(entry, dict):
            continue

        total_series += 1
        ep_count = count_links(entry)
        total_episodes += ep_count

        if ep_count > 0:
            series_with_episodes += 1

        if entry.get("_error"):
            series_with_errors += 1

    return {
        "file": path.name,
        "mode": mode,
        "total_series": total_series,
        "series_with_episodes": series_with_episodes,
        "series_with_errors": series_with_errors,
        "total_episodes": total_episodes,
    }


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summaries = []
    grand_series = 0
    grand_episodes = 0

    for path in sorted(OUTPUT_DIR.glob("*_Tv_Series.json")):
        summary = summarize_file(path)
        summaries.append(summary)
        grand_series += summary["total_series"]
        grand_episodes += summary["total_episodes"]

    index_data = {
        "built_at": utc_now_iso(),
        "output_dir": str(OUTPUT_DIR.name),
        "files": summaries,
        "grand_total_series": grand_series,
        "grand_total_episodes": grand_episodes,
    }

    with INDEX_FILE.open("w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    print(f"Written index: {INDEX_FILE}")


if __name__ == "__main__":
    main()
