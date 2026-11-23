#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Octo-Replay · SEO üreticisi.

Üretir / günceller:
  * index.html içindeki <!--seo:start--> ... <!--seo:end--> bloğu (canonical, Open Graph, Twitter, JSON-LD)
  * etkinlik/index.html                  tüm Türkiye etkinliklerinin listesi
  * etkinlik/{event_key}/index.html      her etkinlik için taranabilir statik sayfa (maçlar, sıralama, takımlar)
  * sitemap.xml ve robots.txt

Etkinlik sayfaları data/tba/ altındaki dosyalardan (tools/fetch_tba.py) üretilir. Tek sayfalık uygulama
hash (#event=...) ile çalıştığı için arama motorları o içeriği ayrı sayfa olarak göremez; bu sayfalar
Google'ın "İstanbul Regional 2026 maçları" gibi aramalarda sizi bulabilmesini sağlar.

Kullanım:
    python tools/seo_build.py --site-url https://KULLANICI.github.io/octo-replay/

--site-url verilmezse SITE_URL ortam değişkeni, o da yoksa index.html'deki mevcut canonical kullanılır.
Betik tekrar çalıştırılabilir (idempotent).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDER_URL = "https://KULLANICI.github.io/octo-replay/"
SITE_NAME = "Octo-Replay"
TITLE = "Octo-Replay · FRC Türkiye Maç Tekrarı ve Analizi"
DESCRIPTION = ("Türkiye FRC maçlarının tekrarını izle; cycle, otonom, savunma ve arıza analizi ile "
               "scouting notları tek ekranda. Fusion Robotics #7682.")
LEVEL_ORDER = {"qm": 1, "ef": 2, "qf": 3, "sf": 4, "f": 5}
LEVEL_NAME = {"qm": "Eleme", "ef": "Sekizlik Final", "qf": "Çeyrek Final", "sf": "Yarı Final", "f": "Final"}
EVENT_KEY_RE = re.compile(r"^\d{4}[a-z0-9]{2,20}$")
MATCH_KEY_RE = re.compile(r"^\d{4}[a-z0-9]{2,20}_[a-z0-9]{2,16}$")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def normalize_site_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://[^\s/]+", url):
        raise SystemExit(f"Hata: --site-url http:// veya https:// ile başlamalı: {url!r}")
    return url if url.endswith("/") else url + "/"


def ld_json(data: Any) -> str:
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + "</script>"


def read_data(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("data")
    except (OSError, ValueError):
        return None


def read_meta_date(path: Path) -> str:
    try:
        stamp = int(json.loads(path.read_text(encoding="utf-8"))["meta"]["fetched_at"])
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
    except (OSError, ValueError, KeyError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fmt_date(iso: str) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", iso or "")
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ""


def date_range(ev: dict) -> str:
    a, b = fmt_date(ev.get("start_date", "")), fmt_date(ev.get("end_date", ""))
    return a if (not b or a == b) else f"{a} – {b}"


def match_label(m: dict) -> str:
    name = LEVEL_NAME.get(m.get("comp_level", ""), str(m.get("comp_level", "")).upper())
    if m.get("comp_level") == "qm":
        return f"{name} {m.get('match_number', '')}"
    return f"{name} {m.get('set_number', '')} · Maç {m.get('match_number', '')}"


def sort_matches(matches: List[dict]) -> List[dict]:
    return sorted(matches, key=lambda m: (LEVEL_ORDER.get(m.get("comp_level", ""), 9), m.get("set_number", 0), m.get("match_number", 0)))


def team_numbers(alliance: dict) -> str:
    return " · ".join(k.replace("frc", "") for k in alliance.get("team_keys", []))


# --- ortak <head> --------------------------------------------------------------------------------

def social_tags(title: str, description: str, url: str, image: str, og_type: str = "website") -> List[str]:
    return [
        f'<link rel="canonical" href="{esc(url)}">',
        f'<meta property="og:type" content="{og_type}">',
        f'<meta property="og:site_name" content="{SITE_NAME}">',
        '<meta property="og:locale" content="tr_TR">',
        f'<meta property="og:title" content="{esc(title)}">',
        f'<meta property="og:description" content="{esc(description)}">',
        f'<meta property="og:url" content="{esc(url)}">',
        f'<meta property="og:image" content="{esc(image)}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="Octo-Replay ve Fusion Robotics ahtapot maskotu">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        f'<meta name="twitter:description" content="{esc(description)}">',
        f'<meta name="twitter:image" content="{esc(image)}">',
    ]


def index_seo_block(site: str) -> str:
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "@id": site + "#website", "url": site, "name": SITE_NAME, "inLanguage": "tr", "description": DESCRIPTION,
             "publisher": {"@id": site + "#team"}},
            {"@type": "WebApplication", "@id": site + "#app", "url": site, "name": SITE_NAME, "applicationCategory": "SportsApplication",
             "operatingSystem": "Web", "inLanguage": "tr", "isAccessibleForFree": True, "description": DESCRIPTION,
             "creator": {"@id": site + "#team"}},
            {"@type": "Organization", "@id": site + "#team", "name": "Fusion Robotics (FRC Team #7682)", "url": site,
             "logo": site + "assets/octopus-logo.png"},
        ],
    }
    lines = ["<!--seo:start-->"] + social_tags(TITLE, DESCRIPTION, site, site + "assets/og-image.png") + [ld_json(graph), "<!--seo:end-->"]
    return "\n  ".join(lines)


def update_index_html(root: Path, site: str) -> None:
    path = root / "index.html"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"<!--seo:start-->.*?<!--seo:end-->", re.DOTALL)
    if not pattern.search(text):
        raise SystemExit("Hata: index.html içinde <!--seo:start--> ... <!--seo:end--> işaretleri yok.")
    block = index_seo_block(site)
    path.write_text(pattern.sub(lambda _m: block, text, count=1), encoding="utf-8")


# --- etkinlik sayfaları --------------------------------------------------------------------------

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#060A1A">
  <meta name="color-scheme" content="dark">
  <title>{title}</title>
  <meta name="description" content="{description}">
  <meta name="robots" content="index,follow,max-image-preview:large">
  <link rel="icon" type="image/png" href="{up}assets/octopus-logo.png">
  <link rel="stylesheet" href="{up}css/style.css">
  {head_extra}
</head>
<body class="seo-page">
  <header class="topbar">
    <a class="brand" href="{up}" aria-label="Octo-Replay ana sayfa">
      <img class="brand-logo" src="{up}assets/octopus-logo.png" alt="" width="44" height="44">
      <span class="brand-text"><strong>Octo-Replay</strong><small>FRC #7682 Fusion Robotics</small></span>
    </a>
    <div class="topbar-actions"><a class="btn btn-small" href="{app_link}">Uygulamada aç</a></div>
  </header>
  <main class="page">
{body}
  </main>
  <footer class="footer"><p>Fusion Robotics #7682 · Maç verileri <a href="https://www.thebluealliance.com" target="_blank" rel="noopener noreferrer">The Blue Alliance</a> API'sinden alınır.</p></footer>
</body>
</html>
"""


def render_page(title: str, description: str, up: str, app_link: str, head_extra: str, body: str) -> str:
    return PAGE_TEMPLATE.format(title=esc(title), description=esc(description), up=up, app_link=esc(app_link),
                                head_extra=head_extra, body=body)


def event_page(site: str, ev: dict, matches: List[dict], teams: List[dict], rankings: dict) -> str:
    key, year = ev["key"], ev.get("year") or key[:4]
    url = f"{site}etkinlik/{key}/"
    place = ", ".join(p for p in (ev.get("city"), ev.get("state_prov"), ev.get("country")) if p)
    dates = date_range(ev)
    kind = "Off-Season" if ev.get("is_offseason") else (ev.get("event_type_string") or "Etkinlik")
    name = ev.get("name") or key
    title = f"{name} {year} · Maçlar ve Analiz | Octo-Replay"
    description = (f"{name} ({place}{', ' if place and dates else ''}{dates}): {len(matches)} maç, {len(teams)} takım. "
                   "Maç tekrarlarını izle; AI analizi ve scouting notlarını gör.")
    nick = {t["team_number"]: t.get("nickname", "") for t in teams}

    event_ld: Dict[str, Any] = {
        "@context": "https://schema.org", "@type": "SportsEvent", "name": f"{name} {year}", "description": description,
        "sport": "FIRST Robotics Competition", "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "url": url, "image": site + "assets/og-image.png", "inLanguage": "tr",
        "location": {"@type": "Place", "name": ev.get("city") or name,
                     "address": {"@type": "PostalAddress", "addressLocality": ev.get("city", ""), "addressRegion": ev.get("state_prov", ""), "addressCountry": "TR"}},
    }
    if ev.get("start_date"):
        event_ld["startDate"] = ev["start_date"]
    if ev.get("end_date"):
        event_ld["endDate"] = ev["end_date"]
    if str(ev.get("website", "")).startswith("http"):
        event_ld["sameAs"] = ev["website"]
    crumbs_ld = {
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": site},
            {"@type": "ListItem", "position": 2, "name": "Etkinlikler", "item": site + "etkinlik/"},
            {"@type": "ListItem", "position": 3, "name": f"{name} {year}", "item": url},
        ],
    }
    head_extra = "\n  ".join(social_tags(title, description, url, site + "assets/og-image.png") + [ld_json(event_ld), ld_json(crumbs_ld)])

    app_link = f"../../#event={key}"
    parts: List[str] = [
        f'    <nav class="crumbs" aria-label="Sayfa yolu"><a href="../../">Ana sayfa</a> <span aria-hidden="true">›</span> <a href="../">Etkinlikler</a> <span aria-hidden="true">›</span> <span>{esc(name)}</span></nav>',
        '    <section class="panel">',
        f'      <p class="eyebrow">{esc(year)}{" · REBUILT" if str(year) == "2026" else ""} · {esc(kind)}</p>',
        f'      <h1 class="match-title">{esc(name)}</h1>',
        f'      <p class="muted">{esc(place)}{" · " if place and dates else ""}{esc(dates)}</p>',
        f'      <p class="seo-lead">{esc(description)}</p>',
        f'      <p><a class="btn" href="{esc(app_link)}">Maçları Octo-Replay\'de aç</a></p>',
        "    </section>",
    ]

    rows = (rankings or {}).get("rankings") or []
    if rows:
        parts += ['    <section class="panel">', '      <h2 class="panel-title">Sıralama</h2>',
                  '      <div class="table-wrap"><table class="seo-table"><thead><tr><th>Sıra</th><th>Takım</th><th>G-M-B</th><th>Oynanan</th></tr></thead><tbody>']
        for r in rows[:20]:
            n = r.get("team_number", 0)
            parts.append(f'        <tr><td>{esc(r.get("rank", ""))}</td><td>{esc(n)} {esc(nick.get(n, ""))}</td>'
                         f'<td>{esc(r.get("wins", 0))}-{esc(r.get("losses", 0))}-{esc(r.get("ties", 0))}</td><td>{esc(r.get("matches_played", 0))}</td></tr>')
        parts += ["      </tbody></table></div>", "    </section>"]

    if matches:
        parts += ['    <section class="panel">', f'      <h2 class="panel-title">Maçlar ({len(matches)})</h2>', '      <ul class="result-list">']
        for m in sort_matches(matches):
            if not MATCH_KEY_RE.match(m.get("key", "")):
                continue
            red, blue = m["alliances"]["red"], m["alliances"]["blue"]
            played = red.get("score") is not None and blue.get("score") is not None and red["score"] >= 0 and blue["score"] >= 0
            score = f'Kırmızı {red["score"]} – {blue["score"]} Mavi' if played else "Oynanmadı"
            video = " · ▶ Video" if any(v.get("type") == "youtube" for v in m.get("videos", [])) else ""
            link = f'../../#event={key}&amp;match={m["key"]}'
            parts.append(f'        <li><a class="result-item" href="{link}"><strong>{esc(match_label(m))}</strong>'
                         f'<span>{esc(score)}{esc(video)}</span><span>K: {esc(team_numbers(red))} · M: {esc(team_numbers(blue))}</span></a></li>')
        parts += ["      </ul>", "    </section>"]

    if teams:
        parts += ['    <section class="panel">', f'      <h2 class="panel-title">Takımlar ({len(teams)})</h2>', '      <ul class="team-chips">']
        for t in teams:
            parts.append(f'        <li><strong>{esc(t["team_number"])}</strong> {esc(t.get("nickname", ""))}</li>')
        parts += ["      </ul>", "    </section>"]
    if not matches:
        parts.append('    <section class="panel"><p class="muted">Bu etkinlik için henüz maç verisi yayımlanmamış.</p></section>')

    return render_page(title, description, "../../", app_link, head_extra, "\n".join(parts))


def events_index_page(site: str, by_year: Dict[str, List[dict]]) -> str:
    url = f"{site}etkinlik/"
    title = "Türkiye FRC Etkinlikleri · Maçlar ve Analiz | Octo-Replay"
    description = "Türkiye'de düzenlenen FIRST Robotics Competition (FRC) resmî ve off-season etkinliklerinin maç listeleri, sıralamaları ve analizleri."
    crumbs_ld = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": SITE_NAME, "item": site},
        {"@type": "ListItem", "position": 2, "name": "Etkinlikler", "item": url}]}
    head_extra = "\n  ".join(social_tags(title, description, url, site + "assets/og-image.png") + [ld_json(crumbs_ld)])
    parts = ['    <nav class="crumbs" aria-label="Sayfa yolu"><a href="../">Ana sayfa</a> <span aria-hidden="true">›</span> <span>Etkinlikler</span></nav>',
             '    <section class="panel">', '      <h1 class="match-title">Türkiye FRC Etkinlikleri</h1>', f'      <p class="seo-lead">{esc(description)}</p>', "    </section>"]
    if not by_year:
        parts.append('    <section class="panel"><p class="muted">Etkinlik verisi henüz yüklenmemiş. Repodaki "Veri güncelle" iş akışı çalıştığında bu sayfa dolar.</p></section>')
    for year in sorted(by_year, reverse=True):
        parts += ['    <section class="panel">', f'      <h2 class="panel-title">{esc(year)} sezonu</h2>', '      <ul class="result-list">']
        for ev in by_year[year]:
            kind = "Off-Season" if ev.get("is_offseason") else (ev.get("event_type_string") or "Etkinlik")
            place = ", ".join(p for p in (ev.get("city"), ev.get("country")) if p)
            parts.append(f'        <li><a class="result-item" href="{esc(ev["key"])}/"><strong>{esc(ev.get("name", ev["key"]))}</strong>'
                         f'<span>{esc(kind)} · {esc(place)}</span><span>{esc(date_range(ev))}</span></a></li>')
        parts += ["      </ul>", "    </section>"]
    return render_page(title, description, "../", "../", head_extra, "\n".join(parts))


def build_pages(root: Path, site: str) -> List[dict]:
    """etkinlik/ sayfalarını yazar; sitemap için [{loc, lastmod, priority}] döndürür."""
    tba = root / "data" / "tba"
    out_dir = root / "etkinlik"
    by_year: Dict[str, List[dict]] = {}
    entries: List[dict] = []
    for year_dir in sorted(p for p in tba.iterdir() if p.is_dir() and p.name.isdigit()) if tba.is_dir() else []:
        events_file = year_dir / "events.json"
        events = read_data(events_file)
        if not isinstance(events, list):
            continue
        lastmod = read_meta_date(events_file)
        by_year[year_dir.name] = [e for e in events if isinstance(e, dict) and EVENT_KEY_RE.match(str(e.get("key", "")))]
        for ev in by_year[year_dir.name]:
            ev_dir = year_dir / ev["key"]
            matches = read_data(ev_dir / "matches.json") or []
            teams = read_data(ev_dir / "teams.json") or []
            rankings = read_data(ev_dir / "rankings.json") or {}
            page = out_dir / ev["key"] / "index.html"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(event_page(site, ev, matches, teams, rankings), encoding="utf-8")
            entries.append({"loc": f"{site}etkinlik/{ev['key']}/", "lastmod": lastmod, "priority": "0.7"})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(events_index_page(site, by_year), encoding="utf-8")
    return entries


def write_sitemap_and_robots(root: Path, site: str, entries: List[dict]) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_entries = [{"loc": site, "lastmod": max([e["lastmod"] for e in entries] or [today]), "priority": "1.0"},
                   {"loc": site + "etkinlik/", "lastmod": max([e["lastmod"] for e in entries] or [today]), "priority": "0.8"}] + entries
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for e in all_entries:
        lines += ["  <url>", f"    <loc>{esc(e['loc'])}</loc>", f"    <lastmod>{e['lastmod']}</lastmod>", f"    <priority>{e['priority']}</priority>", "  </url>"]
    lines.append("</urlset>")
    (root / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (root / "robots.txt").write_text(
        "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /cache/\nDisallow: /dev/\nDisallow: /tools/\n\n"
        f"Sitemap: {site}sitemap.xml\n", encoding="utf-8")


def current_canonical(root: Path) -> Optional[str]:
    try:
        m = re.search(r'<link rel="canonical" href="([^"]+)"', (root / "index.html").read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Octo-Replay SEO üreticisi", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--site-url", default="", help="sitenin tam adresi, örn. https://kullanici.github.io/octo-replay/")
    ap.add_argument("--root", default=str(ROOT), help="proje klasörü (varsayılan: bu betiğin üst klasörü)")
    args = ap.parse_args(argv)
    root = Path(args.root)
    site = normalize_site_url(args.site_url or os.environ.get("SITE_URL", "") or current_canonical(root) or PLACEHOLDER_URL)

    update_index_html(root, site)
    entries = build_pages(root, site)
    write_sitemap_and_robots(root, site, entries)
    print(f"Site adresi: {site}")
    print(f"index.html SEO bloğu, sitemap.xml ({len(entries) + 2} adres), robots.txt ve {len(entries)} etkinlik sayfası yazıldı.")
    if "KULLANICI" in site:
        print("Uyarı: site adresi yer tutucu. --site-url ile gerçek adresi verin (GitHub Actions bunu otomatik yapar).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
