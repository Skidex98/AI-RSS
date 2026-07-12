#!/usr/bin/env python3
"""
AI Digest — agregator RSS/GitHub -> jeden przefiltrowany feed Atom.

Uruchamiany cyklicznie przez GitHub Actions. Wynik: docs/feed.xml + docs/index.html,
serwowane przez GitHub Pages, subskrybowane w Inoreaderze.

Bez zewnętrznych API keys. Bez LLM. Tylko RSS + GitHub public API.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape as xml_escape

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
STATE_FILE = ROOT / "state.json"

USER_AGENT = "ai-digest/1.0 (+https://github.com)"
HTTP_TIMEOUT = 25

# GitHub API — token opcjonalny. GitHub Actions wstrzykuje GITHUB_TOKEN automatycznie,
# co podnosi limit z 60 do 5000 req/h.
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": USER_AGENT,
    "X-GitHub-Api-Version": "2022-11-28",
}
if GH_TOKEN:
    GH_HEADERS["Authorization"] = f"Bearer {GH_TOKEN}"


# ---------------------------------------------------------------- model


@dataclass
class Item:
    uid: str
    title: str
    url: str
    source: str
    category: str          # news | paper | repo | release
    published: str         # ISO 8601
    summary: str = ""
    matched: list[str] = field(default_factory=list)

    @property
    def published_dt(self) -> datetime:
        return datetime.fromisoformat(self.published)


def make_uid(url: str, title: str) -> str:
    return hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()[:16]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- utils


def clean_text(raw: str, limit: int = 320) -> str:
    """Usuwa HTML, skraca, normalizuje białe znaki."""
    if not raw:
        return ""
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"&[a-zA-Z#0-9]+;", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt


def entry_datetime(entry) -> datetime:
    """Wyciąga datę z wpisu feedparsera; fallback: teraz."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
            except (ValueError, OverflowError, TypeError):
                continue
    return now_utc()


class KeywordMatcher:
    """Dopasowanie po granicach słów, case-insensitive."""

    def __init__(self, include: list[str], exclude: list[str]) -> None:
        self.include = [(kw, self._compile(kw)) for kw in include]
        self.exclude = [(kw, self._compile(kw)) for kw in exclude]

    @staticmethod
    def _compile(kw: str) -> re.Pattern:
        # \b nie działa dobrze z myślnikami/kropkami — używamy lookaroundów
        return re.compile(
            r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])",
            re.IGNORECASE,
        )

    def check(self, *texts: str) -> tuple[bool, list[str]]:
        """Zwraca (czy_przepuścić, lista_trafionych_słów)."""
        blob = " ".join(t for t in texts if t).lower()
        if not blob:
            return False, []

        for kw, pattern in self.exclude:
            if pattern.search(blob):
                return False, []

        hits = [kw for kw, pattern in self.include if pattern.search(blob)]
        return bool(hits), hits


# ---------------------------------------------------------------- kolektory


def collect_feeds(cfg: dict, matcher: KeywordMatcher, cutoff: datetime) -> list[Item]:
    items: list[Item] = []

    for feed_cfg in cfg.get("feeds", []):
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        tier = feed_cfg.get("tier", "filtered")

        try:
            resp = requests.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
        except Exception as exc:
            print(f"  [!] {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        if parsed.bozo and not parsed.entries:
            print(f"  [!] {name}: nie udało się sparsować", file=sys.stderr)
            continue

        kept = 0
        for entry in parsed.entries:
            published = entry_datetime(entry)
            if published < cutoff:
                continue

            title = clean_text(entry.get("title", ""), 200)
            link = entry.get("link", "")
            if not title or not link:
                continue

            summary = clean_text(
                entry.get("summary", "") or entry.get("description", "")
            )

            if tier == "always":
                matched = ["*"]
            else:
                ok, matched = matcher.check(title, summary)
                if not ok:
                    continue

            category = "paper" if "arxiv" in url or "papers" in url.lower() else "news"
            if "TrendingRSS" in url:
                category = "repo"

            items.append(
                Item(
                    uid=make_uid(link, title),
                    title=title,
                    url=link,
                    source=name,
                    category=category,
                    published=published.isoformat(),
                    summary=summary,
                    matched=matched,
                )
            )
            kept += 1

        print(f"  {name}: {kept} pozycji")

    return items


def collect_releases(cfg: dict, cutoff: datetime) -> list[Item]:
    """Releases z obserwowanych repo — zawsze przechodzą, bez filtrowania."""
    items: list[Item] = []

    for repo in cfg.get("github_releases", []):
        url = f"https://api.github.com/repos/{repo}/releases?per_page=5"
        try:
            resp = requests.get(url, headers=GH_HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 404:
                print(f"  [!] {repo}: 404 (repo nie istnieje?)", file=sys.stderr)
                continue
            resp.raise_for_status()
            releases = resp.json()
        except Exception as exc:
            print(f"  [!] {repo}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        for rel in releases:
            if rel.get("draft"):
                continue
            raw_date = rel.get("published_at") or rel.get("created_at")
            if not raw_date:
                continue

            published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            if published < cutoff:
                continue

            tag = rel.get("tag_name", "?")
            name = rel.get("name") or tag
            prerelease = " (pre)" if rel.get("prerelease") else ""
            title = f"{repo} {tag}{prerelease} — {name}" if name != tag else f"{repo} {tag}{prerelease}"

            items.append(
                Item(
                    uid=make_uid(rel.get("html_url", ""), tag),
                    title=clean_text(title, 200),
                    url=rel.get("html_url", f"https://github.com/{repo}/releases"),
                    source="GitHub Releases",
                    category="release",
                    published=published.isoformat(),
                    summary=clean_text(rel.get("body", ""), 400),
                    matched=["*"],
                )
            )

    print(f"  releases: {len(items)} pozycji")
    return items


def collect_new_repos(cfg: dict, matcher: KeywordMatcher, cutoff: datetime) -> list[Item]:
    """Nowe repo z GitHub Search API."""
    search_cfg = cfg.get("github_search", {})
    if not search_cfg.get("enabled"):
        return []

    items: list[Item] = []
    seen: set[str] = set()

    min_stars = search_cfg.get("min_stars", 40)
    days = search_cfg.get("created_within_days", 14)
    since = (now_utc() - timedelta(days=days)).strftime("%Y-%m-%d")

    for query in search_cfg.get("queries", []):
        q = f"{query} created:>{since} stars:>={min_stars}"
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "order": "desc", "per_page": 15},
                headers=GH_HEADERS,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            results = resp.json().get("items", [])
        except Exception as exc:
            print(f"  [!] search '{query}': {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        for repo in results:
            full_name = repo.get("full_name", "")
            if full_name in seen:
                continue
            seen.add(full_name)

            desc = repo.get("description") or ""
            ok, matched = matcher.check(full_name, desc)
            if not ok:
                continue

            pushed = repo.get("pushed_at") or repo.get("created_at")
            published = datetime.fromisoformat(pushed.replace("Z", "+00:00"))

            stars = repo.get("stargazers_count", 0)
            lang = repo.get("language") or "?"

            items.append(
                Item(
                    uid=make_uid(repo.get("html_url", ""), full_name),
                    title=f"⭐ {stars} · {full_name} ({lang})",
                    url=repo.get("html_url", ""),
                    source="GitHub — nowe repo",
                    category="repo",
                    published=published.isoformat(),
                    summary=clean_text(desc, 300),
                    matched=matched,
                )
            )

        time.sleep(1)  # search API ma osobny, niższy rate limit

    print(f"  nowe repo: {len(items)} pozycji")
    return items


# ---------------------------------------------------------------- stan


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": [], "items": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": [], "items": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


# ---------------------------------------------------------------- output


CATEGORY_LABEL = {
    "news": "NEWS",
    "paper": "PAPER",
    "repo": "REPO",
    "release": "RELEASE",
}

CATEGORY_EMOJI = {
    "news": "📰",
    "paper": "📄",
    "repo": "📦",
    "release": "🚀",
}


def build_atom(items: list[Item], cfg: dict) -> str:
    out_cfg = cfg["output"]
    site = out_cfg["site_url"].rstrip("/")
    updated = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom">',
        f"  <title>{xml_escape(out_cfg['title'])}</title>",
        f"  <subtitle>{xml_escape(out_cfg['description'])}</subtitle>",
        f'  <link href="{site}/feed.xml" rel="self"/>',
        f'  <link href="{site}/"/>',
        f"  <id>{site}/</id>",
        f"  <updated>{updated}</updated>",
        "  <author><name>ai-digest</name></author>",
    ]

    for it in items:
        emoji = CATEGORY_EMOJI.get(it.category, "•")
        label = CATEGORY_LABEL.get(it.category, it.category.upper())
        published = it.published_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        tags = ""
        if it.matched and it.matched != ["*"]:
            tags = "<p><small>tagi: " + ", ".join(it.matched[:8]) + "</small></p>"

        body = (
            f"<p><strong>[{label}]</strong> {xml_escape(it.source)}</p>"
            f"<p>{xml_escape(it.summary)}</p>"
            f"{tags}"
            f'<p><a href="{xml_escape(it.url)}">Otwórz →</a></p>'
        )

        parts += [
            "  <entry>",
            f"    <title>{emoji} {xml_escape(it.title)}</title>",
            f'    <link href="{xml_escape(it.url)}"/>',
            f"    <id>urn:uuid:{it.uid}</id>",
            f"    <updated>{published}</updated>",
            f"    <published>{published}</published>",
            f'    <category term="{label}"/>',
            f'    <content type="html">{xml_escape(body)}</content>',
            "  </entry>",
        ]

    parts.append("</feed>")
    return "\n".join(parts)


def build_html(items: list[Item], cfg: dict) -> str:
    """Prosty podgląd w przeglądarce — przydaje się do sanity-checku."""
    out_cfg = cfg["output"]
    site = out_cfg["site_url"].rstrip("/")
    generated = now_utc().strftime("%Y-%m-%d %H:%M UTC")

    by_cat: dict[str, list[Item]] = {}
    for it in items:
        by_cat.setdefault(it.category, []).append(it)

    rows = []
    for cat in ("release", "news", "repo", "paper"):
        cat_items = by_cat.get(cat, [])
        if not cat_items:
            continue
        emoji = CATEGORY_EMOJI.get(cat, "•")
        rows.append(
            f'<h2>{emoji} {CATEGORY_LABEL.get(cat, cat)} '
            f'<span class="count">{len(cat_items)}</span></h2>'
        )
        for it in cat_items[:40]:
            date = it.published_dt.strftime("%d.%m")
            rows.append(
                '<article>'
                f'<a href="{xml_escape(it.url)}" target="_blank" rel="noopener">'
                f'{xml_escape(it.title)}</a>'
                f'<div class="meta">{xml_escape(it.source)} · {date}</div>'
                f'<p>{xml_escape(it.summary)}</p>'
                '</article>'
            )

    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{xml_escape(out_cfg['title'])}</title>
<link rel="alternate" type="application/atom+xml" href="{site}/feed.xml">
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font: 16px/1.55 -apple-system, "Segoe UI", Roboto, sans-serif;
    max-width: 46rem; margin: 0 auto; padding: 1.5rem 1rem 4rem;
    background: #fbfbfa; color: #1c1c1a;
  }}
  header {{ border-bottom: 2px solid #1c1c1a; padding-bottom: .75rem; margin-bottom: 1.5rem; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
  .sub {{ color: #6b6b66; font-size: .85rem; }}
  .sub code {{ background: #ececea; padding: .1rem .35rem; border-radius: 3px; font-size: .8rem; }}
  h2 {{ font-size: .78rem; letter-spacing: .1em; text-transform: uppercase;
       color: #6b6b66; margin: 2rem 0 .75rem; font-weight: 600; }}
  .count {{ background: #ececea; color: #6b6b66; padding: .05rem .4rem;
           border-radius: 8px; font-size: .7rem; margin-left: .3rem; }}
  article {{ padding: .8rem 0; border-bottom: 1px solid #e6e6e3; }}
  article a {{ color: #1c1c1a; font-weight: 500; text-decoration: none; }}
  article a:hover {{ text-decoration: underline; }}
  .meta {{ font-size: .75rem; color: #8a8a84; margin-top: .2rem; }}
  article p {{ font-size: .875rem; color: #55554f; margin: .35rem 0 0; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #17171a; color: #e8e8e4; }}
    header {{ border-color: #e8e8e4; }}
    article {{ border-color: #2c2c30; }}
    article a {{ color: #e8e8e4; }}
    .count, .sub code {{ background: #2c2c30; }}
    article p {{ color: #a0a09a; }}
  }}
</style>
</head>
<body>
<header>
  <h1>{xml_escape(out_cfg['title'])}</h1>
  <div class="sub">
    {len(items)} pozycji · zaktualizowano {generated}<br>
    RSS: <code>{site}/feed.xml</code>
  </div>
</header>
{"".join(rows) or "<p>Brak pozycji.</p>"}
</body>
</html>"""


# ---------------------------------------------------------------- main


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    matcher = KeywordMatcher(
        cfg["keywords"].get("include", []),
        cfg["keywords"].get("exclude", []),
    )

    lookback = cfg["output"].get("lookback_hours", 48)
    cutoff = now_utc() - timedelta(hours=lookback)

    print(f"── AI Digest · {now_utc():%Y-%m-%d %H:%M UTC} ──")
    print(f"Okno: ostatnie {lookback}h\n")

    print("Feedy RSS:")
    fresh = collect_feeds(cfg, matcher, cutoff)

    print("\nGitHub:")
    fresh += collect_releases(cfg, cutoff)
    fresh += collect_new_repos(cfg, matcher, cutoff)

    # Deduplikacja względem historii
    state = load_state()
    seen: set[str] = set(state.get("seen", []))

    new_items = [it for it in fresh if it.uid not in seen]
    print(f"\nNowych (po dedup): {len(new_items)}")

    # Scal z archiwum
    archive = [Item(**d) for d in state.get("items", [])]
    combined = new_items + archive

    # Dedup w obrębie scalonego zbioru + sort malejąco po dacie
    uniq: dict[str, Item] = {}
    for it in combined:
        uniq.setdefault(it.uid, it)

    final = sorted(uniq.values(), key=lambda x: x.published_dt, reverse=True)
    final = final[: cfg["output"].get("max_items", 120)]

    # Zapis
    DOCS.mkdir(exist_ok=True)
    (DOCS / "feed.xml").write_text(build_atom(final, cfg), encoding="utf-8")
    (DOCS / "index.html").write_text(build_html(final, cfg), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    save_state(
        {
            "seen": [it.uid for it in final][:600],
            "items": [asdict(it) for it in final],
            "last_run": now_utc().isoformat(),
        }
    )

    print(f"Feed: {len(final)} pozycji → docs/feed.xml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
