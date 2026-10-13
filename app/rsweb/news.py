"""Game news: the official changelog (https://replicant.space/changelog/) and blog (https://replicant.space/blog/),
read every 6 hours (engine stage news_pass). New changelog versions and blog posts go to the bell and the log; the
dashboard shows the latest. The first read only sets the baseline (no notifications for history).

The pages are server-rendered HTML (Astro): changelog entries are <li class="changelog-entry" id="vX.Y.Z"> with
entry-version / entry-codename / <time datetime>, and an <article class="entry-body">; blog posts are links to
/blog/{n}/ with a title, an author and a <time datetime>."""
from __future__ import annotations

import html
import re

SITE = "https://replicant.space"
KV = "game_news"
EVERY = 6 * 3600
KEEP = 40


def _text(fragment: str) -> str:
    t = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", fragment)
    t = re.sub(r"<li[^>]*>", "• ", t)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", t)).strip()


def parse_changelog(page: str) -> list[dict]:
    """[{version, codename, date, summary, sections: {heading: [items]}, text}], newest first."""
    out = []
    for m in re.finditer(r'<li class="changelog-entry" id="v([^"]+)"(.*?)</li>\s*(?=<li class="changelog-entry"|</ol>)', page, re.S):
        body = m.group(2)
        code = re.search(r'class="entry-codename"[^>]*>([^<]*)<', body)
        date = re.search(r'<time[^>]*datetime="([^"]+)"', body)
        art = re.search(r'<article class="entry-body"[^>]*>(.*?)</article>', body, re.S)
        art = art.group(1) if art else ""
        intro = art.split("<h3", 1)[0]
        sections = {}
        for h, rest in re.findall(r"<h3[^>]*>(.*?)</h3>(.*?)(?=<h3|$)", art, re.S):
            sections[_text(h)] = [_text(li) for li in re.findall(r"<li[^>]*>(.*?)</li>", rest, re.S)]
        out.append({"version": m.group(1), "codename": (code.group(1).strip() if code else ""),
                    "date": date.group(1) if date else "", "summary": _text(intro), "sections": sections,
                    "url": f"{SITE}/changelog/#v{m.group(1)}"})
    return out


def parse_blog(page: str) -> list[dict]:
    """[{id, title, author, date, url}], newest first (the featured post, then the list)."""
    out, seen = [], set()
    feat = re.search(r'<article class="featured".*?<a href="/blog/(\d+)/"[^>]*>(.*?)</a>.*?byline-author[^>]*>(.*?)<.*?'
                     r'<time datetime="([^"]+)"', page, re.S)
    if feat:
        out.append({"id": feat.group(1), "title": _text(feat.group(2)), "author": _text(feat.group(3)), "date": feat.group(4)})
        seen.add(feat.group(1))
    for m in re.finditer(r'<a href="/blog/(\d+)/" class="post-card".*?<time[^>]*datetime="([^"]+)".*?class="post-title"[^>]*>(.*?)</h3>'
                         r'.*?class="post-author"[^>]*>(.*?)</span>', page, re.S):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            out.append({"id": m.group(1), "title": _text(m.group(3)), "author": _text(m.group(4)), "date": m.group(2)})
    for p in out:
        p["url"] = f"{SITE}/blog/{p['id']}/"
    return sorted(out, key=lambda p: (p["date"], int(p["id"])), reverse=True)


def new_items(state: dict, changelog: list[dict], blog: list[dict]) -> tuple[list[dict], list[dict]]:
    """What wasn't there at the last read (nothing on the first read: that's the baseline)."""
    if not state.get("checked_at"):
        return [], []
    old_v = set(state.get("known_versions") or []) | {c["version"] for c in state.get("changelog") or []}
    old_b = set(state.get("known_posts") or []) | {p["id"] for p in state.get("blog") or []}
    return [c for c in changelog if c["version"] not in old_v], [p for p in blog if p["id"] not in old_b]


async def fetch(path: str) -> str:
    import httpx
    async with httpx.AsyncClient(base_url=SITE, timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "replicant-space-frontend (news check)"}) as c:
        r = await c.get(path)
        r.raise_for_status()
        return r.text


def headline(c: dict) -> str:
    """One line for the bell: the version and its first sentence."""
    first = re.split(r"(?<=[.!?])\s", c.get("summary") or "", maxsplit=1)[0][:160]
    return f"Game update v{c['version']}" + (f" ({c['codename']})" if c.get("codename") else "") + (f": {first}" if first else "")
