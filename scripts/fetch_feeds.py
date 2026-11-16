#!/usr/bin/env python3
"""Collect all news feeds listed in sources.json into data/feeds.json.

Runs on GitHub Actions every few minutes. Uses only the Python standard
library. For each source it tries the site's own RSS feed first; if that
fails or is empty, it falls back to a Google News feed limited to that site.

Tabs (categories) can have topics ("subs"). Each article gets a "tags" list:
from a topic feed (source "feeds"), from words in its link ("url"), and from
keywords in its title and summary ("kw", "re"). Untagged articles count as
the tab's "other" topic in the app.
"""
import html
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MAX_PER_SOURCE = 40           # per feed: the source's main feed and each topic feed
TOPIC_MAX_AGE = 3 * 86400     # some topic feeds are rarely updated; skip their old articles
DESC_LEN = 400
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Mobile Safari/537.36 Mivzakonim/1.0"

MEDIA_NS = "{http://search.yahoo.com/mrss/}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
KNOWN_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def google_news_url(site, lang):
    q = f"site:{site} when:1d"
    if lang == "en":
        params = {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    else:
        params = {"q": q, "hl": "iw", "gl": "IL", "ceid": "IL:he"}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


# Google News answers "429 Too Many Requests" when several searches arrive at once,
# so Google requests are sent one at a time with a short gap between them.
GOOGLE_GAP = 2.0
_google_lock = threading.Lock()
_google_last = 0.0


def download(url):
    global _google_last
    if url.startswith("https://news.google.com/"):
        with _google_lock:
            wait = _google_last + GOOGLE_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            _google_last = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def clean_text(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def first_img(raw_html):
    if not raw_html:
        return ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', html.unescape(raw_html), re.I)
    return m.group(1) if m else ""


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        d = None
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
            try:
                d = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
                break
            except Exception:
                continue
    if d is None:
        return None
    if d.tzinfo is None:
        # Israeli sites often omit the time zone; assume Israel time
        d = d.replace(tzinfo=ZoneInfo("Asia/Jerusalem"))
    return int(d.timestamp())


def text_of(el, *names):
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def parse_feed(data):
    """Return list of dicts: title, link, date, desc, img."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # some feeds have stray characters; try a lenient cleanup
        txt = data.decode("utf-8", "replace")
        txt = re.sub(r"&(?!(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", txt)
        txt = re.sub(r"<\?xml[^>]*\?>", "", txt, count=1)
        # some feeds (e.g. ice) use atom:/dc:/media: tags without declaring them
        missing = [p for p in KNOWN_NS if f"<{p}:" in txt and f"xmlns:{p}=" not in txt]
        if missing:
            decl = "".join(f' xmlns:{p}="{KNOWN_NS[p]}"' for p in missing)
            txt = re.sub(r"<(rss|feed|rdf:RDF)\b", lambda m: m.group(0) + decl, txt, count=1)
        root = ET.fromstring(txt.encode("utf-8"))

    items = []
    rss_items = root.findall(".//item")
    if rss_items:
        for it in rss_items:
            raw_desc = text_of(it, "description", CONTENT_NS + "encoded")
            img = ""
            enc = it.find("enclosure")
            if enc is not None and "image" in (enc.get("type") or "image"):
                img = enc.get("url") or ""
            if not img:
                for tag in (MEDIA_NS + "content", MEDIA_NS + "thumbnail"):
                    m = it.find(tag)
                    if m is not None and m.get("url"):
                        img = m.get("url")
                        break
            if not img:
                img = text_of(it, "image")
            if not img:
                img = first_img(raw_desc)
            items.append({
                "title": clean_text(text_of(it, "title")),
                "link": text_of(it, "link", "guid"),
                "date": parse_date(text_of(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date", "pubdate")),
                "desc": clean_text(raw_desc)[:DESC_LEN],
                "img": img,
            })
    else:
        for it in root.findall(ATOM_NS + "entry"):
            link_el = it.find(ATOM_NS + "link")
            raw_desc = text_of(it, ATOM_NS + "summary", ATOM_NS + "content")
            items.append({
                "title": clean_text(text_of(it, ATOM_NS + "title")),
                "link": link_el.get("href") if link_el is not None else "",
                "date": parse_date(text_of(it, ATOM_NS + "updated", ATOM_NS + "published")),
                "desc": clean_text(raw_desc)[:DESC_LEN],
                "img": first_img(raw_desc),
            })
    return [i for i in items if i["title"] and i["link"]]


def fetch_feed(attempts, only=None):
    """Try each (via, url) in turn; return (via, items) or raise the last error.

    only: for a site-wide feed, keep just links containing one of these, e.g. ".../news/".
    """
    err = "empty"
    for via, url in attempts:
        try:
            items = parse_feed(download(url))
            if only:
                items = [i for i in items if any(o in i["link"] for o in only)]
            if not items:
                err = "empty"
                continue
            if via == "google":
                for i in items:
                    # Google News titles end with " - Site name"; its descriptions repeat the title
                    i["title"] = re.sub(r"\s+[-–]\s+[^-–]{1,40}$", "", i["title"])
                    i["desc"] = ""
                items = [i for i in items if i["title"] and not i["title"].startswith(("-", "–"))]
            items.sort(key=lambda i: i["date"] or 0, reverse=True)
            return via, items[:MAX_PER_SOURCE]
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:200]
    raise RuntimeError(err)


def collect(src):
    attempts = []
    if src.get("url"):
        attempts.append(("rss", src["url"]))
    if src.get("fallback"):
        attempts.append(("google", google_news_url(src["fallback"], src.get("lang", "he"))))
    by_link, via, err = {}, "", ""

    def add(items, tags):
        for i in items:
            i["src"] = src["id"]
            kept = by_link.setdefault(i["link"], i)
            kept.setdefault("tags", set()).update(tags)

    try:
        via, items = fetch_feed(attempts, src.get("only"))
        add(items, src.get("tags", []) if via == "rss" else [])
    except Exception as e:  # noqa: BLE001
        err = str(e)
    topics_ok = 0
    now = time.time()
    for feed in src.get("feeds", []):
        try:
            _, items = fetch_feed([("rss", feed["url"])])
        except Exception:  # noqa: BLE001
            continue  # a dead topic feed shouldn't take the whole source down
        items = [i for i in items if not i["date"] or now - i["date"] < TOPIC_MAX_AGE]
        topics_ok += 1
        add(items, feed["tags"])
    items = list(by_link.values())
    if not items:
        return src["id"], {"ok": False, "error": err or "empty"}, []
    st = {"ok": True, "via": via or "topics", "count": len(items)}
    if src.get("feeds"):
        st["topics"] = f"{topics_ok}/{len(src['feeds'])}"
    return src["id"], st, items


HEB = "\u05d0-\u05ea"
PREFIX = "[והבלמשכ]"


def keyword_pattern(kw):
    """Regex for one keyword.

    Hebrew words may take up to two prefix letters (ו ה ב ל מ ש כ), so "כדורסל"
    also matches "בכדורסל". A trailing * allows any ending (a stem); otherwise the
    word must end there, so "גול" doesn't match "גולן". A leading = means no prefixes.
    """
    exact = kw.startswith("=")
    stem = kw.endswith("*")
    word = kw.strip("=*")
    body = re.escape(word)
    if re.match(f"[{HEB}]", word):
        pre = "" if exact else f"{PREFIX}{{0,2}}"
    else:
        # Latin terms: case-insensitive whole words, optionally after a Hebrew prefix like "ב-"
        pre = "" if exact else f"(?:{PREFIX}{{1,2}}-?)?"
        body = f"(?i:{body})"
    left = f"(?<![{HEB}A-Za-z0-9]){pre}"
    right = "" if stem else f"(?![{HEB}A-Za-z0-9])"
    return left + body + right


def build_topics(categories):
    """cat id -> list of (sub id, link substrings, compiled text regex or None)."""
    topics = {}
    for cat in categories:
        rules = []
        for sub in cat.get("subs", []):
            if sub["id"] == "other":
                continue
            parts = [keyword_pattern(k) for k in sub.get("kw", [])] + sub.get("re", [])
            rx = re.compile("|".join(parts)) if parts else None
            rules.append((sub["id"], [u.lower() for u in sub.get("url", [])], rx))
        if rules:
            topics[cat["id"]] = rules
    return topics


def tag_item(item, rules):
    tags = item.pop("tags", set())
    tags &= {sid for sid, _, _ in rules}  # a feed tag only counts if the tab has that topic
    link = item["link"].lower()
    # the headline and the start of the summary; deeper in the summary there are too many side mentions
    text = item["title"] + " " + item["desc"][:160]
    for sid, urls, rx in rules:
        if any(u in link for u in urls) or (rx and rx.search(text)):
            tags.add(sid)
    if tags:
        item["tags"] = sorted(tags)


def main():
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(collect, cfg["sources"]))
    status, items, seen = {}, [], {}
    for sid, st, its in results:
        status[sid] = st
        for i in its:
            key = i["link"]
            if key in seen:
                seen[key]["tags"] |= i["tags"]
                continue
            seen[key] = i
            items.append(i)
    cat_of = {s["id"]: s["cat"] for s in cfg["sources"]}
    topics = build_topics(cfg["categories"])
    for i in items:
        rules = topics.get(cat_of[i["src"]])
        if rules:
            tag_item(i, rules)
        else:
            i.pop("tags", None)
    # the app only needs each topic's id and name, not the matching rules
    categories = []
    for c in cfg["categories"]:
        c = dict(c)
        if c.get("subs"):
            c["subs"] = [{"id": x["id"], "name": x["name"]} for x in c["subs"]]
        categories.append(c)
    out = {
        "updated": int(time.time()),
        "categories": categories,
        "sources": [{k: s.get(k) for k in ("id", "name", "cat", "lang")} | status[s["id"]] for s in cfg["sources"]],
        "items": items,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "feeds.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for s in out["sources"]:
        print(("OK  " if s["ok"] else "FAIL"), s["id"], s.get("via", ""), s.get("count", s.get("error", "")), s.get("topics", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
