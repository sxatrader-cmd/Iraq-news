#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
مراقب الأخبار العراقية — يجمع آخر الأخبار من عدة صحف عراقية
ويحدّث صفحة HTML واحدة تلقائياً.

يشتغل عبر launchd كل 30 دقيقة (راجع ملف التنصيب المرفق).
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from html import escape, unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------
# الإعدادات
# ---------------------------------------------------------------

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "seen_urls.json"
OUTPUT_HTML = BASE_DIR / "iraq_news.html"
LOG_FILE = BASE_DIR / "watcher.log"

MAX_ITEMS_KEPT = 150          # أقصى عدد أخبار تُحفظ بالصفحة
MAX_NEW_PER_RUN = 12          # أقصى عدد أخبار جديدة تُجلب بكل تشغيلة لكل مصدر
REQUEST_TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"
}

# كل مصدر: صفحة تجميع الأخبار + نمط الروابط المقبولة كـ"خبر"
SOURCES = [
    {
        "name": "شفق نيوز",
        "listing_url": "https://shafaq.com/ar/كل-الاخبار",
        "base": "https://shafaq.com",
        "link_pattern": re.compile(r"^https://shafaq\.com/ar/[^/]+/[^/]+$"),
        "exclude_pattern": re.compile(r"/tags/|/bbc-arabic/"),
    },
    {
        "name": "بغداد اليوم",
        "listing_url": "https://baghdadtoday.news/",
        "base": "https://baghdadtoday.news",
        "link_pattern": re.compile(r"^https://baghdadtoday\.news/\d{4}/\d{2}/\d{2}/.+"),
        "exclude_pattern": None,
    },
    {
        "name": "السومرية نيوز",
        "listing_url": "https://www.alsumaria.tv/news",
        "base": "https://www.alsumaria.tv",
        "link_pattern": re.compile(r"^https://www\.alsumaria\.tv/news/\d+/.+"),
        "exclude_pattern": None,
    },
    {
        "name": "المدى",
        "listing_url": "https://almadapaper.net/",
        "base": "https://almadapaper.net",
        "link_pattern": re.compile(r"^https://almadapaper\.net/\d+/\d+/\d+/.+"),
        "exclude_pattern": None,
    },
]


# ---------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"seen": [], "items": []}
    return {"seen": [], "items": []}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch(url: str):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def get_meta(soup: BeautifulSoup, prop: str, attr: str = "property"):
    tag = soup.find("meta", attrs={attr: prop})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def extract_article(url: str, source_name: str):
    """يفتح صفحة الخبر ويستخرج العنوان والتفاصيل."""
    try:
        html = fetch(url)
    except Exception as e:
        log(f"  تعذر فتح {url}: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    title = (
        get_meta(soup, "og:title")
        or (soup.title.string.strip() if soup.title and soup.title.string else None)
    )
    if soup.find("h1"):
        h1_text = soup.find("h1").get_text(strip=True)
        if h1_text:
            title = h1_text

    description = get_meta(soup, "og:description") or get_meta(
        soup, "description", attr="name"
    )

    if not description:
        # احتياط: أول فقرة نص طويلة شوي من محتوى الصفحة
        for p in soup.find_all("p"):
            txt = p.get_text(strip=True)
            if len(txt) > 60:
                description = txt
                break

    canonical = get_meta(soup, "og:url") or url
    link_tag = soup.find("link", rel="canonical")
    if link_tag and link_tag.get("href"):
        canonical = link_tag["href"]

    if not title:
        return None

    return {
        "title": title.strip(),
        "details": (description or "").strip()[:600],
        "source": source_name,
        "url": canonical,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def discover_links(source: dict):
    """يرجع لستة روابط أخبار محتملة من صفحة التجميع لمصدر معين."""
    try:
        html = fetch(source["listing_url"])
    except Exception as e:
        log(f"تعذر فتح صفحة {source['name']}: {e}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen_local = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(source["base"], a["href"].strip())
        href = href.split("#")[0]

        if source["exclude_pattern"] and source["exclude_pattern"].search(href):
            continue
        if not source["link_pattern"].match(href):
            continue
        if href in seen_local:
            continue

        seen_local.add(href)
        links.append(href)

    return links


# ---------------------------------------------------------------
# بناء صفحة HTML
# ---------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>آخر أخبار العراق</title>
<style>
  :root {{
    --navy: #0b1220;
    --navy-light: #121b2e;
    --gold: #c9a86a;
    --text: #e9e6df;
    --muted: #9aa3b5;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, "SF Arabic", "Tahoma", sans-serif;
    background: var(--navy);
    color: var(--text);
    padding-bottom: 40px;
  }}
  header {{
    position: sticky;
    top: 0;
    background: linear-gradient(180deg, var(--navy) 80%, transparent);
    padding: 18px 16px 10px;
    z-index: 10;
    border-bottom: 1px solid rgba(201,168,106,0.25);
  }}
  header h1 {{
    margin: 0;
    font-size: 20px;
    color: var(--gold);
    letter-spacing: 0.5px;
  }}
  header .updated {{
    font-size: 12px;
    color: var(--muted);
    margin-top: 4px;
  }}
  .list {{
    padding: 12px;
    max-width: 720px;
    margin: 0 auto;
  }}
  .card {{
    background: var(--navy-light);
    border: 1px solid rgba(201,168,106,0.15);
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 12px;
  }}
  .card .source {{
    display: inline-block;
    font-size: 11px;
    color: var(--navy);
    background: var(--gold);
    padding: 3px 10px;
    border-radius: 20px;
    margin-bottom: 8px;
    font-weight: 600;
  }}
  .card h2 {{
    font-size: 17px;
    margin: 0 0 8px;
    line-height: 1.5;
  }}
  .card p {{
    font-size: 14px;
    color: var(--muted);
    line-height: 1.7;
    margin: 0 0 10px;
  }}
  .card a.readmore {{
    font-size: 13px;
    color: var(--gold);
    text-decoration: none;
  }}
  .card .time {{
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
    display: block;
  }}
  .empty {{
    text-align: center;
    color: var(--muted);
    padding: 40px 16px;
  }}
</style>
</head>
<body>
<header>
  <h1>آخر أخبار العراق</h1>
  <div class="updated">آخر تحديث: {updated_at}</div>
</header>
<div class="list">
{items_html}
</div>
</body>
</html>
"""

CARD_TEMPLATE = """<div class="card">
  <span class="source">{source}</span>
  <h2>{title}</h2>
  <p>{details}</p>
  <a class="readmore" href="{url}" target="_blank" rel="noopener">قراءة الخبر كاملاً من المصدر ←</a>
  <span class="time">{time_label}</span>
</div>"""


def render_html(items):
    if not items:
        body = '<div class="empty">لا توجد أخبار محفوظة بعد.</div>'
    else:
        cards = []
        for it in items:
            try:
                dt = datetime.fromisoformat(it["fetched_at"])
                time_label = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                time_label = ""
            cards.append(
                CARD_TEMPLATE.format(
                    source=escape(it["source"]),
                    title=escape(it["title"]),
                    details=escape(it["details"]),
                    url=escape(it["url"], quote=True),
                    time_label=time_label,
                )
            )
        body = "\n".join(cards)

    html = PAGE_TEMPLATE.format(
        updated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        items_html=body,
    )
    OUTPUT_HTML.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------
# التشغيل الرئيسي
# ---------------------------------------------------------------

def run():
    state = load_state()
    seen = set(state.get("seen", []))
    items = state.get("items", [])

    total_new = 0

    for source in SOURCES:
        log(f"جاري فحص: {source['name']}")
        links = discover_links(source)
        new_links = [l for l in links if l not in seen][:MAX_NEW_PER_RUN]

        if not new_links:
            log(f"  لا يوجد أخبار جديدة من {source['name']}")
            continue

        for url in new_links:
            article = extract_article(url, source["name"])
            time.sleep(1)  # حتى ما نضغط على السيرفر
            if not article:
                continue
            seen.add(url)
            items.insert(0, article)
            total_new += 1
            log(f"  + جديد: {article['title'][:60]}")

    items = items[:MAX_ITEMS_KEPT]

    state["seen"] = list(seen)[-2000:]  # تفادي تضخم الملف بلا حدود
    state["items"] = items
    save_state(state)

    render_html(items)
    log(f"انتهى التشغيل — أخبار جديدة: {total_new} — إجمالي محفوظ: {len(items)}")


if __name__ == "__main__":
    run()
