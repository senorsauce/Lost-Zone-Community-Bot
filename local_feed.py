#!/usr/bin/env python3
"""Serve a small local news and events RSS dashboard."""

from __future__ import annotations

import argparse
import html
import json
import threading
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable
from xml.etree import ElementTree


USER_AGENT = "ZVTBOT local feed/1.0"


@dataclass(frozen=True)
class FeedItem:
    title: str
    link: str
    summary: str
    published: str
    source: str


def _text(element: ElementTree.Element | None, default: str = "") -> str:
    return (element.text or default).strip() if element is not None else default


def parse_rss(payload: bytes, limit: int = 20) -> list[FeedItem]:
    """Parse common RSS 2.0 and Atom entries into the app's small feed model."""
    root = ElementTree.fromstring(payload)
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    items: list[FeedItem] = []
    for entry in entries[:limit]:
        title = _text(entry.find("title")) or _text(entry.find("{http://www.w3.org/2005/Atom}title"))
        link_element = entry.find("link")
        link = _text(link_element)
        if not link and link_element is not None:
            link = link_element.attrib.get("href", "")
        if not link:
            link = _text(entry.find("{http://www.w3.org/2005/Atom}link"))
        description = _text(entry.find("description")) or _text(
            entry.find("{http://www.w3.org/2005/Atom}summary")
        )
        published = _text(entry.find("pubDate")) or _text(
            entry.find("{http://www.w3.org/2005/Atom}published")
        ) or _text(entry.find("{http://www.w3.org/2005/Atom}updated"))
        source_element = entry.find("source")
        source = _text(source_element) or urllib.parse.urlparse(link).netloc
        if title and link:
            items.append(FeedItem(title, link, description, published, source))
    return items


def google_news_url(query: str) -> str:
    params = urllib.parse.urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return f"https://news.google.com/rss/search?{params}"


def fetch_items(url: str, timeout: float = 8.0) -> list[FeedItem]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_rss(response.read())


def load_feeds(location: str, distance: int) -> tuple[list[FeedItem], list[FeedItem]]:
    """Return news and event results using public Google News RSS search."""
    news_query = f'"{location}" local news'
    event_query = f'events near "{location}" within {distance} miles'
    results: list[list[FeedItem]] = []
    for query in (news_query, event_query):
        try:
            results.append(fetch_items(google_news_url(query)))
        except (OSError, ElementTree.ParseError):
            results.append([])
    return results[0], results[1]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Dispatch</title><style>
:root{color-scheme:light;--ink:#18231f;--muted:#68736d;--paper:#f5f1e8;--card:#fffdf8;--accent:#d65b3d;--line:#d8d4c9}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 Georgia,serif}
main{max-width:980px;margin:auto;padding:42px 22px 70px}.masthead{display:flex;justify-content:space-between;gap:24px;align-items:end;border-bottom:2px solid var(--ink);padding-bottom:22px}
h1{font:700 clamp(2.5rem,7vw,5.6rem)/.85 Georgia,serif;letter-spacing:-.05em;margin:0}.eyebrow{font:700 11px/1.2 Arial,sans-serif;letter-spacing:.16em;text-transform:uppercase;color:var(--accent);margin:0 0 12px}.context{color:var(--muted);text-align:right;max-width:260px}
.tabs{display:flex;gap:20px;border-bottom:1px solid var(--line);margin:28px 0 0}.tab{background:none;border:0;border-bottom:3px solid transparent;color:var(--muted);cursor:pointer;font:700 15px Arial,sans-serif;padding:13px 3px}.tab.active{border-color:var(--accent);color:var(--ink)}
.feed{display:grid;gap:1px;margin-top:18px}.item{background:var(--card);border-top:1px solid var(--line);padding:19px 18px;display:grid;grid-template-columns:1fr auto;gap:8px 20px}.item h2{font:700 1.3rem/1.15 Georgia,serif;margin:0}.item h2 a{color:inherit;text-decoration:none}.item h2 a:hover{color:var(--accent)}.item p{color:var(--muted);grid-column:1/-1;margin:0}.meta{color:var(--accent);font:11px Arial,sans-serif;letter-spacing:.08em;text-transform:uppercase;text-align:right}.empty{color:var(--muted);padding:35px 0}.rss{color:var(--accent);font:12px Arial,sans-serif;text-decoration:none}
@media(max-width:600px){.masthead{display:block}.context{text-align:left;margin-top:18px}.item{display:block}.meta{margin-top:10px;text-align:left}}
</style></head><body><main><header class="masthead"><div><p class="eyebrow">A local reading desk</p><h1>Local Dispatch</h1></div><div class="context" id="context"></div></header>
<nav class="tabs" aria-label="Feed sections"><button class="tab active" data-feed="news">News</button><button class="tab" data-feed="events">Events</button><a class="rss" id="rss" href="/feed/news.xml">RSS</a></nav><section class="feed" id="feed" aria-live="polite"></section></main>
<script>
const context=document.querySelector('#context'), feed=document.querySelector('#feed'), rss=document.querySelector('#rss');
const escapeHtml=value=>String(value).replace(/[&<>'"]/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
async function showFeed(name){document.querySelectorAll('.tab').forEach(tab=>tab.classList.toggle('active',tab.dataset.feed===name));rss.href=`/feed/${name}.xml`;feed.innerHTML='<p class="empty">Refreshing the desk...</p>';const response=await fetch(`/api/${name}`);const data=await response.json();feed.innerHTML=data.items.length?data.items.map(item=>`<article class="item"><h2><a href="${escapeHtml(item.link)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a></h2><div class="meta">${escapeHtml(item.source)}</div><p>${escapeHtml(item.summary)}</p></article>`).join(''):'<p class="empty">No results arrived. Check your connection and refresh.</p>';}
fetch('/api/config').then(response=>response.json()).then(data=>context.textContent=`${data.location} · within ${data.distance} miles`);document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>showFeed(tab.dataset.feed)));showFeed('news');
</script></body></html>"""


def rss_document(title: str, link: str, items: Iterable[FeedItem]) -> bytes:
    channel = ElementTree.Element("channel")
    ElementTree.SubElement(channel, "title").text = title
    ElementTree.SubElement(channel, "link").text = link
    ElementTree.SubElement(channel, "description").text = title
    for item in items:
        element = ElementTree.SubElement(channel, "item")
        for name, value in (("title", item.title), ("link", item.link), ("description", item.summary), ("pubDate", item.published), ("source", item.source)):
            ElementTree.SubElement(element, name).text = value
    root = ElementTree.Element("rss", version="2.0")
    root.append(channel)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


class FeedHandler(BaseHTTPRequestHandler):
    location = ""
    distance = 25
    feeds: tuple[list[FeedItem], list[FeedItem]] = ([], [])

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(PAGE.encode())
            return
        if path == "/api/config":
            self.send_json({"location": self.location, "distance": self.distance}); return
        index = {"/api/news": 0, "/api/events": 1, "/feed/news.xml": 0, "/feed/events.xml": 1}
        if path in index:
            items = self.feeds[index[path]]
            if path.startswith("/api/"):
                self.send_json({"items": [asdict(item) for item in items]})
            else:
                body = rss_document(f"Local Dispatch: {path.split('/')[2]}", self.location, items)
                self.send_response(200); self.send_header("Content-Type", "application/rss+xml; charset=utf-8"); self.end_headers(); self.wfile.write(body)
            return
        self.send_error(404)

    def send_json(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers(); self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch a local news and events RSS dashboard")
    parser.add_argument("--location", required=True, help="City, ZIP code, or state to search")
    parser.add_argument("--distance", type=int, default=25, help="Event search radius in miles (default: 25)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the dashboard automatically")
    args = parser.parse_args()
    if args.distance < 1:
        parser.error("--distance must be at least 1 mile")
    print(f"Loading local feeds for {args.location} within {args.distance} miles...")
    FeedHandler.location, FeedHandler.distance = args.location, args.distance
    FeedHandler.feeds = load_feeds(args.location, args.distance)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), FeedHandler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Local Dispatch is running at {url} (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Local Dispatch.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()