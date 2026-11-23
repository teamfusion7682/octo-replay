#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Octo-Replay · TBA verisini statik JSON olarak indirir (GitHub Pages gibi PHP'siz barındırma için).

Bir sezonun Türkiye etkinliklerini (off-season dahil) ve her etkinliğin maçlarını, takımlarını ve
sıralamalarını `data/tba/{yıl}/` altına yazar. Çıktı, api/tba-proxy.php'nin döndürdüğü yapıyla
BİREBİR aynıdır; ön yüz API yoksa bu dosyaları okur.

    data/tba/2026/events.json
    data/tba/2026/{event_key}/matches.json
    data/tba/2026/{event_key}/teams.json
    data/tba/2026/{event_key}/rankings.json

Kullanım:
    export TBA_API_KEY=...            (Windows: set TBA_API_KEY=...)
    python tools/fetch_tba.py --year 2026
    python tools/fetch_tba.py --year 2026 --event 2026tuis

Veri değişmediyse dosyaya dokunulmaz (gereksiz git değişikliği oluşmaz).
GitHub Actions ile otomatik çalışması için .github/workflows/update-data.yml'e bakın.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyzer as A  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "tba"
TEAM_KEY_RE = re.compile(r"^frc\d{1,5}$")


# --- api/tba-proxy.php dönüştürücülerinin birebir karşılığı ---------------------------------

def _s(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value) if isinstance(value, (str, int, float)) else ""


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return default


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _numeric(value: Any) -> Any:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return None
    return None


def map_event(e: dict) -> dict:
    event_type = _int(e.get("event_type"), -1)
    return {
        "key": _s(e.get("key")),
        "name": _s(e.get("name")),
        "short_name": _s(e.get("short_name") if e.get("short_name") is not None else e.get("name")),
        "city": _s(e.get("city")),
        "state_prov": _s(e.get("state_prov")),
        "country": _s(e.get("country")),
        "event_type": event_type,
        "event_type_string": _s(e.get("event_type_string")),
        "playoff_type": _int_or_none(e.get("playoff_type")),
        "start_date": _s(e.get("start_date")),
        "end_date": _s(e.get("end_date")),
        "week": _int_or_none(e.get("week")),
        "year": _int(e.get("year"), 0),
        "website": _s(e.get("website")),
        "is_offseason": event_type in (99, 100),
    }


def transform_events(raw: Any) -> List[dict]:
    out = []
    for event in raw if isinstance(raw, list) else []:
        if isinstance(event, dict) and A.is_turkey_event(event):
            mapped = map_event(event)
            if mapped["key"]:
                out.append(mapped)
    out.sort(key=lambda m: (m["start_date"], m["name"]))
    return out


def transform_teams(raw: Any) -> List[dict]:
    out = []
    for team in raw if isinstance(raw, list) else []:
        if not isinstance(team, dict):
            continue
        number = _int(team.get("team_number"), 0)
        if number <= 0:
            continue
        out.append({
            "key": f"frc{number}",
            "team_number": number,
            "nickname": _s(team.get("nickname")),
            "city": _s(team.get("city")),
            "state_prov": _s(team.get("state_prov")),
            "country": _s(team.get("country")),
        })
    out.sort(key=lambda t: t["team_number"])
    return out


def _team_number(key: str) -> int:
    digits = re.sub(r"\D+", "", key)
    return int(digits) if digits else 0


def transform_rankings(raw: Any) -> dict:
    result: Dict[str, list] = {"sort_order_info": [], "rankings": []}
    if not isinstance(raw, dict):
        return result
    for info in raw.get("sort_order_info") or []:
        result["sort_order_info"].append(_s(info.get("name")) if isinstance(info, dict) else "")
    for row in raw.get("rankings") or []:
        if not isinstance(row, dict):
            continue
        team_key = _s(row.get("team_key"))
        record = row.get("record") if isinstance(row.get("record"), dict) else {}
        sort_orders = row.get("sort_orders") if isinstance(row.get("sort_orders"), list) else []
        result["rankings"].append({
            "rank": _int(row.get("rank"), 0),
            "team_key": team_key,
            "team_number": _team_number(team_key),
            "wins": _int(record.get("wins"), 0),
            "losses": _int(record.get("losses"), 0),
            "ties": _int(record.get("ties"), 0),
            "matches_played": _int(row.get("matches_played"), 0),
            "dq": _int(row.get("dq"), 0),
            "sort_orders": [_numeric(v) for v in sort_orders],
        })
    return result


def _map_alliance(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    keys = [k for k in (raw.get("team_keys") if isinstance(raw.get("team_keys"), list) else [])
            if isinstance(k, str) and TEAM_KEY_RE.match(k)]
    score = _numeric(raw.get("score"))
    strings = lambda v: [x for x in (v if isinstance(v, list) else []) if isinstance(x, str)]  # noqa: E731
    return {
        "team_keys": keys,
        "score": None if score is None else int(score),
        "surrogate_team_keys": strings(raw.get("surrogate_team_keys")),
        "dq_team_keys": strings(raw.get("dq_team_keys")),
    }


def transform_matches(raw: Any) -> List[dict]:
    out = []
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        videos = []
        for video in m.get("videos") if isinstance(m.get("videos"), list) else []:
            if isinstance(video, dict) and "type" in video and isinstance(video.get("key"), str):
                videos.append({"type": _s(video["type"]), "key": video["key"]})
        alliances = m.get("alliances") if isinstance(m.get("alliances"), dict) else {}
        mapped = {
            "key": _s(m.get("key")),
            "event_key": _s(m.get("event_key")),
            "comp_level": _s(m.get("comp_level")),
            "set_number": _int(m.get("set_number"), 0),
            "match_number": _int(m.get("match_number"), 0),
            "alliances": {"red": _map_alliance(alliances.get("red")), "blue": _map_alliance(alliances.get("blue"))},
            "winning_alliance": _s(m.get("winning_alliance")),
            "time": _int_or_none(m.get("time")),
            "predicted_time": _int_or_none(m.get("predicted_time")),
            "actual_time": _int_or_none(m.get("actual_time")),
            "post_result_time": _int_or_none(m.get("post_result_time")),
            "videos": videos,
        }
        if mapped["key"]:
            out.append(mapped)
    return out


# --- Yazma -----------------------------------------------------------------------------------

def write_if_changed(path: Path, data: Any) -> bool:
    """Veri değiştiyse (meta hariç) dosyayı yazar. Yazıldıysa True döner."""
    if path.is_file():
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("data") == data:
                return False
        except (ValueError, OSError):
            pass
    payload = {
        "ok": True,
        "data": data,
        "meta": {"static": True, "cached": True, "stale": False, "fetched_at": int(time.time()), "ttl": 0},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def fetch_event(client: "A.TbaClient", key: str, year_dir: Path) -> List[str]:
    """Etkinliğin üç dosyasını indirir; değişen dosya adlarını döndürür. Hata A.AnalyzerError olarak yükselir."""
    changed: List[str] = []
    for name, endpoint, transform in (
        ("matches", f"/event/{key}/matches", transform_matches),
        ("teams", f"/event/{key}/teams/simple", transform_teams),
        ("rankings", f"/event/{key}/rankings", transform_rankings),
    ):
        raw = client.get(endpoint)  # 404 -> None -> boş yapı (etkinlik henüz veri yayımlamamış olabilir)
        if write_if_changed(year_dir / key / f"{name}.json", transform(raw)):
            changed.append(name)
    return changed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="TBA verisini statik JSON olarak indir", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--event", action="append", help="yalnızca bu etkinlik(ler) (varsayılan: tüm Türkiye etkinlikleri)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="çıktı klasörü (varsayılan: data/tba)")
    ap.add_argument("--api-key", default="", help="TBA anahtarı (varsayılan: TBA_API_KEY ortam değişkeni)")
    args = ap.parse_args(argv)

    try:
        client = A.TbaClient(args.api_key or os.environ.get("TBA_API_KEY", ""))
        raw_events = client.get(f"/events/{args.year}")
        if not isinstance(raw_events, list):
            raise A.AnalyzerError(f"{args.year} etkinlik listesi alınamadı.")
    except A.AnalyzerError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 2

    events = transform_events(raw_events)
    year_dir = Path(args.out) / str(args.year)
    if args.event:
        wanted = set(args.event)
        events_to_fetch = [e for e in events if e["key"] in wanted]
        missing = wanted - {e["key"] for e in events_to_fetch}
        if missing:
            print(f"Uyarı: Türkiye etkinlikleri arasında bulunamadı: {', '.join(sorted(missing))}", file=sys.stderr)
    else:
        events_to_fetch = events
    print(f"{args.year}: {len(events)} Türkiye etkinliği (off-season dahil).")
    if not events:
        print("TBA'da bu sezon için Türkiye etkinliği yok; dosya yazılmadı.", file=sys.stderr)
        return 1

    if not args.event and write_if_changed(year_dir / "events.json", events):
        print("  events.json güncellendi")
    failed = 0
    for event in events_to_fetch:
        key = event["key"]
        if not A.EVENT_KEY_RE.match(key):
            continue
        try:
            changed = fetch_event(client, key, year_dir)
            print(f"  {key:<14} {event['name'][:40]:<40} " + (", ".join(changed) + " güncellendi" if changed else "değişiklik yok"))
        except A.AnalyzerError as exc:
            failed += 1
            print(f"  {key}: HATA {exc}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
