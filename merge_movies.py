import os
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# =============================================
# কনফিগারেশন
# =============================================
BASE_DIRS = ['Movies', 'movies', 'Movie', 'movie']
TIMEOUT = 8
MAX_WORKERS = 20
OUTPUT_ONLINE = 'all_movies.json'
OUTPUT_OFFLINE = 'offline.json'

# =============================================
# Standard ফরম্যাট — যেকোনো item কে normalize করা
# =============================================
def normalize_item(item):
    """
    যেকোনো ফরম্যাটের item কে standard ফরম্যাটে রূপান্তর করা।
    
    Standard output:
    {
        "title": "...",
        "url": "...",
        "thumbnail": "...",
        "category": "...",
        "year": "...",
        "description": "...",
        "quality": "...",
        "language": "..."
    }
    """
    if not isinstance(item, dict):
        return None

    # URL বের করা — সব সম্ভাব্য ফিল্ড নাম চেক
    url = (
        item.get('url') or
        item.get('stream_url') or
        item.get('streamUrl') or
        item.get('stream') or
        item.get('link') or
        item.get('src') or
        item.get('source') or
        item.get('videoUrl') or
        item.get('video_url') or
        item.get('playUrl') or
        item.get('play_url') or
        item.get('hls_url') or
        item.get('hlsUrl') or
        item.get('mp4') or
        item.get('file') or
        ''
    ).strip()

    if not url:
        return None

    # Title বের করা
    title = (
        item.get('title') or
        item.get('name') or
        item.get('movie_name') or
        item.get('movieName') or
        item.get('label') or
        item.get('channel_name') or
        item.get('channelName') or
        item.get('show_name') or
        'Unknown'
    ).strip()

    # Thumbnail বের করা
    thumbnail = (
        item.get('thumbnail') or
        item.get('poster') or
        item.get('image') or
        item.get('img') or
        item.get('cover') or
        item.get('banner') or
        item.get('logo') or
        item.get('icon') or
        item.get('thumb') or
        item.get('backdrop') or
        ''
    ).strip()

    # Category বের করা
    category = (
        item.get('category') or
        item.get('genre') or
        item.get('type') or
        item.get('genres') or
        item.get('group') or
        item.get('group_title') or
        item.get('section') or
        ''
    )
    if isinstance(category, list):
        category = ', '.join(str(c) for c in category)
    category = str(category).strip()

    # Year বের করা
    year = (
        item.get('year') or
        item.get('release_year') or
        item.get('releaseYear') or
        item.get('release_date') or
        item.get('releaseDate') or
        ''
    )
    year = str(year).strip()[:4] if year else ''

    # Description বের করা
    description = (
        item.get('description') or
        item.get('desc') or
        item.get('overview') or
        item.get('plot') or
        item.get('synopsis') or
        item.get('summary') or
        ''
    ).strip()

    # Quality বের করা
    quality = (
        item.get('quality') or
        item.get('resolution') or
        item.get('hd') or
        ''
    )
    quality = str(quality).strip()

    # Language বের করা
    language = (
        item.get('language') or
        item.get('lang') or
        item.get('audio') or
        ''
    ).strip()

    # Standard ফরম্যাটে return
    normalized = {
        'title': title,
        'url': url,
    }

    # খালি না হলেই যোগ করা
    if thumbnail:
        normalized['thumbnail'] = thumbnail
    if category:
        normalized['category'] = category
    if year:
        normalized['year'] = year
    if description:
        normalized['description'] = description
    if quality:
        normalized['quality'] = quality
    if language:
        normalized['language'] = language

    return normalized

# =============================================
# যেকোনো JSON স্ট্রাকচার থেকে আইটেম বের করা
# =============================================
def extract_items(data, depth=0):
    """
    যেকোনো JSON structure থেকে সব মুভি/স্ট্রিম আইটেম বের করা।
    Recursive — nested structure ও handle করবে।
    """
    if depth > 5:  # অনেক গভীরে গেলে থামবে
        return []

    items = []

    if isinstance(data, list):
        for element in data:
            if isinstance(element, dict):
                # এটি কি একটি মিডিয়া আইটেম?
                url = (
                    element.get('url') or element.get('stream_url') or
                    element.get('link') or element.get('src') or
                    element.get('source') or element.get('streamUrl') or
                    element.get('videoUrl') or element.get('video_url') or
                    element.get('hls_url') or element.get('mp4') or
                    element.get('file') or element.get('play_url')
                )
                if url:
                    items.append(element)
                else:
                    # nested list বা dict হতে পারে
                    items.extend(extract_items(element, depth + 1))
            elif isinstance(element, list):
                items.extend(extract_items(element, depth + 1))

    elif isinstance(data, dict):
        # সরাসরি URL আছে কিনা চেক
        url = (
            data.get('url') or data.get('stream_url') or
            data.get('link') or data.get('src') or
            data.get('source') or data.get('streamUrl') or
            data.get('videoUrl') or data.get('video_url') or
            data.get('hls_url') or data.get('mp4') or
            data.get('file') or data.get('play_url')
        )
        if url:
            items.append(data)
        else:
            # সব value এর মধ্যে খোঁজা
            for key, value in data.items():
                if isinstance(value, (list, dict)):
                    items.extend(extract_items(value, depth + 1))

    return items

# =============================================
# ডিবাগ স্ক্যান
# =============================================
def debug_scan():
    print("\n" + "="*60)
    print("🔎 DEBUG: রুট ডিরেক্টরি:")
    for item in sorted(os.listdir('.')):
        kind = '📁' if os.path.isdir(item) else '📄'
        size = f" ({os.path.getsize(item)} bytes)" if os.path.isfile(item) else ''
        print(f"  {kind} {item}{size}")

    print("\n🔎 DEBUG: Movies ফোল্ডার স্ট্রাকচার:")
    found = False
    for base in BASE_DIRS:
        if not os.path.exists(base):
            continue
        found = True
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            level = root.replace(base, '').count(os.sep)
            indent = '  ' * level
            print(f"{indent}📁 {os.path.basename(root)}/")
            for f in files:
                fpath = os.path.join(root, f)
                size = os.path.getsize(fpath)
                print(f"{indent}  📄 {f} ({size} bytes)")
                if f.endswith('.json') and size > 0:
                    try:
                        with open(fpath, 'r', encoding='utf-8', errors='replace') as fp:
                            preview = fp.read(200).replace('\n', ' ')
                        print(f"{indent}     👀 {preview[:120]!r}")
                    except Exception as e:
                        print(f"{indent}     ❌ {e}")
    if not found:
        print("  ❌ Movies ফোল্ডার নেই!")
    print("="*60 + "\n")

# =============================================
# URL অনলাইন চেক (HEAD + GET fallback)
# =============================================
def check_link(item):
    url = item.get('url', '').strip()
    if not url or not url.startswith('http'):
        return item, False, "no_url"

    headers = {
        'User-Agent': 'Mozilla/5.0 (compatible; StreamChecker/1.0)',
        'Accept': '*/*',
    }

    try:
        resp = requests.head(url, timeout=TIMEOUT, headers=headers,
                             allow_redirects=True)
        if resp.status_code < 400:
            return item, True, resp.status_code

        resp = requests.get(
            url, timeout=TIMEOUT,
            headers={**headers, 'Range': 'bytes=0-0'},
            stream=True, allow_redirects=True
        )
        if resp.status_code in (200, 206):
            return item, True, resp.status_code
        return item, False, resp.status_code

    except requests.exceptions.SSLError:
        try:
            http_url = url.replace('https://', 'http://', 1)
            resp = requests.head(http_url, timeout=TIMEOUT, headers=headers,
                                 allow_redirects=True, verify=False)
            if resp.status_code < 400:
                return item, True, f"ssl_fallback_{resp.status_code}"
        except:
            pass
        return item, False, "ssl_error"
    except requests.exceptions.ConnectionError:
        return item, False, "connection_error"
    except requests.exceptions.Timeout:
        return item, False, "timeout"
    except Exception as e:
        return item, False, str(e)[:50]

# =============================================
# মূল প্রসেস
# =============================================
def merge_process():
    debug_scan()

    base_dir = None
    for d in BASE_DIRS:
        if os.path.exists(d) and os.path.isdir(d):
            base_dir = d
            break

    if not base_dir:
        print("❌ Movies ফোল্ডার পাওয়া যায়নি!")
        for f in [OUTPUT_ONLINE, OUTPUT_OFFLINE]:
            with open(f, 'w', encoding='utf-8') as fp:
                json.dump([], fp, indent=2)
        return

    print(f"📂 স্ক্যান: '{base_dir}'")
    print("="*50)

    all_normalized = []
    seen_urls = set()
    scanned = 0

    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for file in files:
            if not file.lower().endswith('.json'):
                continue

            file_path = os.path.join(root, file)
            scanned += 1
            print(f"\n  🔍 [{scanned}] {file_path}")

            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()

                if not content:
                    print(f"      ⚠️  খালি ফাইল")
                    continue

                if content.startswith('\ufeff'):
                    content = content[1:]

                try:
                    data = json.loads(content)
                except json.JSONDecodeError as e:
                    print(f"      ❌ JSON ত্রুটি: {e}")
                    continue

                # যেকোনো ফরম্যাট থেকে আইটেম বের করা
                raw_items = extract_items(data)
                print(f"      📦 Raw আইটেম পাওয়া গেছে: {len(raw_items)}টি")

                new_count = 0
                for raw in raw_items:
                    normalized = normalize_item(raw)
                    if normalized is None:
                        continue
                    url = normalized['url']
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_normalized.append(normalized)
                        new_count += 1

                print(f"      ✅ {new_count} নতুন unique আইটেম যোগ হলো")

            except Exception as e:
                print(f"      ❌ সমস্যা: {e}")

    print(f"\n{'='*50}")
    print(f"📊 মোট unique আইটেম: {len(all_normalized)}")

    if not all_normalized:
        print("\n⚠️  কোনো আইটেম পাওয়া যায়নি!")
        print("   ➡️  JSON এ url/stream_url/link/src ফিল্ড থাকতে হবে")
        for fname in [OUTPUT_ONLINE, OUTPUT_OFFLINE]:
            with open(fname, 'w', encoding='utf-8') as fp:
                json.dump([], fp, indent=2)
        return

    # লিঙ্ক চেক
    print(f"\n🌐 লিঙ্ক চেক ({len(all_normalized)}টি) — {MAX_WORKERS} থ্রেড...")

    online = []
    offline = []
    errors = {}
    checked = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_link, item): item
                   for item in all_normalized}
        for future in as_completed(futures):
            checked += 1
            if checked % 20 == 0 or checked == len(all_normalized):
                print(f"   ⏳ {checked}/{len(all_normalized)} "
                      f"| ✅ {len(online)} | ❌ {len(offline)}")
            try:
                item, is_online, status = future.result()
                if is_online:
                    online.append(item)
                else:
                    offline.append(item)
                    errors[str(status)] = errors.get(str(status), 0) + 1
            except Exception:
                offline.append(futures[future])

    # Standard ফরম্যাটে সেভ
    with open(OUTPUT_ONLINE, 'w', encoding='utf-8') as f:
        json.dump(online, f, indent=2, ensure_ascii=False)

    with open(OUTPUT_OFFLINE, 'w', encoding='utf-8') as f:
        json.dump(offline, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*50}")
    print(f"🎉 সম্পন্ন!")
    print(f"   ✅ অনলাইন  : {len(online)} টি  →  {OUTPUT_ONLINE}")
    print(f"   ❌ অফলাইন  : {len(offline)} টি  →  {OUTPUT_OFFLINE}")
    if errors:
        print(f"   📋 ত্রুটির ধরন:")
        for err, count in sorted(errors.items(), key=lambda x: -x[1]):
            print(f"      {err}: {count}টি")

    # Sample দেখানো
    if online:
        print(f"\n📋 প্রথম ৩টি অনলাইন আইটেম:")
        for item in online[:3]:
            print(f"   🎬 {item.get('title')} → {item.get('url', '')[:60]}...")

if __name__ == "__main__":
    merge_process()
