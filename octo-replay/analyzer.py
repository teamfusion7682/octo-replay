#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Octo-Replay · yerel analiz scripti (FRC #7682 Fusion Robotics)

BU SCRİPT SUNUCUDA ÇALIŞTIRILMAZ. Kendi bilgisayarınızda çalıştırın, çıkan
`data/` klasörünü hosting'e yükleyin.

Yaptığı iş
  1. The Blue Alliance (TBA) API v3'ten Türkiye etkinliklerini (country == Turkey/Türkiye,
     off-season dahil) ve maçların YouTube video kimliklerini çeker.
  2. Maç videosunu yt-dlp ile düşük çözünürlükte indirir, kırmızı/mavi bumperlı robotları
     tespit eder (YOLOv8 özel ağırlıkları veya OpenCV HSV renk yedeği) ve takip eder.
  3. Takip izlerinden cycle süresi, otonom hareketi, savunma ve hareketsizlik (arıza)
     aralıklarını çıkarıp `data/{yıl}/{event_key}/{match_key}.json` olarak yazar.

Kurulum:   pip install -r requirements.txt
Anahtar:   export TBA_API_KEY=...        (Windows: set TBA_API_KEY=...)

Örnekler
  python analyzer.py events --year 2026
  python analyzer.py analyze --event 2026tuis --dry-run
  python analyzer.py analyze --event 2026tuis --weights models/bumpers.pt
  python analyzer.py analyze --match 2026tuis_qm12 --force
  python analyzer.py all --year 2026 --weights models/bumpers.pt

ÖNEMLİ SINIRLAR (dürüst not)
  * `--weights` verilmezse yalnızca renk tabanlı yedek dedektör kullanılır; bu dedektör saha
    şeritleri, ışıklar ve seyirci gibi kırmızı/mavi nesnelerden yanılabilir. Doğruluk için
    kendi bumper veri kümenizle eğitilmiş bir YOLOv8 modeli kullanın. Sınıf adları "red"/"blue"
    içermelidir (örn. red_bumper, blue_bumper); ya da genel "robot" sınıfı kullanılırsa
    ittifak rengi kutunun alt kısmındaki bumper renginden belirlenir.
  * Takım numarası OCR ile okunmaz. Takip izleri, ittifak içinde başlangıç konumuna (üstten
    alta) göre TBA'daki takım sırasına eşlenir ("station_order_heuristic"). Çıkan JSON'daki
    `identity_method` alanı bunu belirtir; yayında takımlar yanlış eşleşebilir, yayınlamadan
    önce örnekleyerek kontrol edin.
  * Yayın kamerası değişirse (replay, yakın çekim) iz kopar; `track_coverage` düşük olur.
  * Cycle, saha kalibrasyonu olmadan "en çok ziyaret edilen durak noktasına art arda
    dönüşler arası süre" olarak tanımlanır; REBUILT'e özgü skor bölgesi bilgisi kullanmaz.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("octo-analyzer")

ANALYZER_VERSION = "1.0.0"
SCHEMA_VERSION = 1
TBA_BASE_URL = "https://www.thebluealliance.com/api/v3"
TURKEY_NAMES = {"turkey", "türkiye", "turkiye"}
EVENT_KEY_RE = re.compile(r"^\d{4}[a-z0-9]{2,20}$")
MATCH_KEY_RE = re.compile(r"^\d{4}[a-z0-9]{2,20}_[a-z0-9]{2,16}$")
YOUTUBE_ID_RE = re.compile(r"^[\w-]{6,20}$")
DATA_DIR = Path(__file__).resolve().parent / "data"

Point = Tuple[float, float]
Pts = Dict[float, Point]


class AnalyzerError(RuntimeError):
    """Beklenen, kullanıcıya gösterilebilir hata."""


class SkipVideo(AnalyzerError):
    """Video bilinçli olarak atlandı (örn. çok uzun yayın)."""


@dataclass(frozen=True)
class Params:
    """Tüm eşikler burada. Mesafeler kare genişliğine, hızlar genişlik/sn'ye göre normalize edilir."""
    sample_fps: float = 5.0
    frame_width: int = 640
    auto_s: float = 20.0            # REBUILT otonom süresi
    match_s: float = 160.0          # otonom 20 sn + teleop 140 sn
    move_speed: float = 0.02        # üstü: hareketli
    still_speed: float = 0.012      # altı: duruyor
    speed_window_s: float = 1.0
    dwell_min_s: float = 0.8
    downtime_min_s: float = 6.0
    cluster_radius: float = 0.06
    cycle_min_s: float = 4.0
    cycle_max_s: float = 60.0
    defense_dist: float = 0.08
    defense_min_s: float = 3.0
    track_max_jump: float = 0.12    # örnek başına en çok yer değiştirme
    track_lost_s: float = 2.0
    min_track_s: float = 5.0
    max_video_s: float = 600.0      # bundan uzun videolar tam yayındır, atlanır
    auto_move_min: float = 0.08     # otonomda en az bu yol alınmışsa "hareketli"


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def rnd(value: Optional[float], digits: int = 2) -> Optional[float]:
    """JSON'a NaN/inf sızmasın diye yuvarlama."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, allow_nan=False)
        fh.write("\n")
    os.replace(tmp, path)


def team_number(team_key: str) -> int:
    digits = re.sub(r"\D+", "", team_key or "")
    return int(digits) if digits else 0


def output_path(year: int, event_key: str, match_key: str) -> Path:
    if not EVENT_KEY_RE.match(event_key) or not MATCH_KEY_RE.match(match_key):
        raise AnalyzerError(f"Geçersiz anahtar: {event_key!r} / {match_key!r}")
    return DATA_DIR / str(year) / event_key / f"{match_key}.json"


# ---------------------------------------------------------------------------
# TBA istemcisi
# ---------------------------------------------------------------------------

class TbaClient:
    def __init__(self, api_key: str, timeout: float = 20.0, retries: int = 3) -> None:
        if not api_key:
            raise AnalyzerError("TBA_API_KEY ortam değişkeni tanımlı değil (veya --api-key verin).")
        try:
            import requests  # noqa: WPS433 (gecikmeli içe aktarma)
        except ImportError as exc:
            raise AnalyzerError("requests kurulu değil: pip install requests") from exc
        self._requests = requests
        self._session = requests.Session()
        self._session.headers.update({
            "X-TBA-Auth-Key": api_key,
            "Accept": "application/json",
            "User-Agent": "Octo-Replay-Analyzer/1.0 (FRC 7682 Fusion Robotics)",
        })
        self._timeout = timeout
        self._retries = max(1, retries)

    def get(self, path: str):
        """JSON döndürür; 404 -> None. Geçici hatalarda yeniden dener."""
        url = TBA_BASE_URL + path
        last_error = "bilinmeyen hata"
        for attempt in range(1, self._retries + 1):
            try:
                resp = self._session.get(url, timeout=self._timeout)
            except self._requests.RequestException as exc:
                last_error = f"ağ hatası: {exc}"
                log.warning("TBA %s (deneme %d/%d): %s", path, attempt, self._retries, last_error)
                time.sleep(min(8, 2 ** attempt))
                continue
            status = resp.status_code
            if status == 404:
                return None
            if status in (401, 403):
                raise AnalyzerError("TBA API anahtarı reddedildi (HTTP %d)." % status)
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
                log.warning("TBA %s (deneme %d/%d): %s", path, attempt, self._retries, last_error)
                time.sleep(min(15, 3 * attempt))
                continue
            if status != 200:
                raise AnalyzerError(f"TBA beklenmeyen yanıt verdi: HTTP {status} · {path}")
            try:
                return resp.json()
            except ValueError as exc:
                raise AnalyzerError(f"TBA geçersiz JSON döndürdü · {path}") from exc
        raise AnalyzerError(f"TBA'ya ulaşılamadı · {path} · {last_error}")


def is_turkey_event(event: dict) -> bool:
    country = str(event.get("country") or "").strip().lower()
    return country in TURKEY_NAMES


def list_turkey_events(client: TbaClient, year: int) -> List[dict]:
    data = client.get(f"/events/{year}")
    if not isinstance(data, list):
        raise AnalyzerError(f"{year} etkinlik listesi alınamadı.")
    events = [e for e in data if isinstance(e, dict) and is_turkey_event(e) and EVENT_KEY_RE.match(str(e.get("key", "")))]
    events.sort(key=lambda e: (str(e.get("start_date") or ""), str(e.get("key"))))
    return events


def first_youtube_id(match: dict) -> str:
    for video in match.get("videos") or []:
        if isinstance(video, dict) and video.get("type") == "youtube" and YOUTUBE_ID_RE.match(str(video.get("key", ""))):
            return str(video["key"])
    return ""


def simplify_match(raw: dict) -> dict:
    """TBA maçını yalnızca gereken alanlara indirger."""
    alliances = raw.get("alliances") or {}
    out = {
        "key": str(raw.get("key", "")),
        "event_key": str(raw.get("event_key", "")),
        "comp_level": str(raw.get("comp_level", "")),
        "set_number": int(raw.get("set_number") or 0),
        "match_number": int(raw.get("match_number") or 0),
        "youtube_id": first_youtube_id(raw),
        "alliances": {},
    }
    for side in ("red", "blue"):
        keys = (alliances.get(side) or {}).get("team_keys") or []
        out["alliances"][side] = {"team_keys": [k for k in keys if isinstance(k, str) and re.match(r"^frc\d+$", k)]}
    return out


def match_sort_key(match: dict):
    order = {"qm": 1, "ef": 2, "qf": 3, "sf": 4, "f": 5}
    return (order.get(match["comp_level"], 9), match["set_number"], match["match_number"])


def fetch_event_matches(client: TbaClient, event_key: str) -> List[dict]:
    data = client.get(f"/event/{event_key}/matches")
    if data is None:
        raise AnalyzerError(f"Etkinlik bulunamadı: {event_key}")
    if not isinstance(data, list):
        raise AnalyzerError(f"Maç listesi geçersiz: {event_key}")
    matches = [simplify_match(m) for m in data if isinstance(m, dict) and m.get("key")]
    matches.sort(key=match_sort_key)
    return matches


# ---------------------------------------------------------------------------
# Tespit
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    alliance: str  # "red" | "blue"
    cx: float      # kare genişliğine göre normalize
    cy: float      # kare genişliğine göre normalize (izotropik mesafe için)
    conf: float


def color_masks(cv2, np, hsv):
    red = cv2.inRange(hsv, np.array([0, 120, 70]), np.array([10, 255, 255])) | \
          cv2.inRange(hsv, np.array([170, 120, 70]), np.array([180, 255, 255]))
    blue = cv2.inRange(hsv, np.array([100, 120, 70]), np.array([130, 255, 255]))
    return {"red": red, "blue": blue}


class ColorDetector:
    """OpenCV HSV renk bölütleme yedeği (özel YOLO ağırlığı yoksa)."""
    name = "hsv-color"

    def __init__(self, cv2, np, max_per_alliance: int = 3) -> None:
        self.cv2, self.np, self.max_n = cv2, np, max_per_alliance
        self.kernel = np.ones((5, 5), np.uint8)

    def detect(self, frame) -> List[Detection]:
        cv2, np = self.cv2, self.np
        h, w = frame.shape[:2]
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        out: List[Detection] = []
        for alliance, mask in color_masks(cv2, np, hsv).items():
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            blobs = []
            for contour in contours:
                x, y, bw, bh = cv2.boundingRect(contour)
                area = float(cv2.contourArea(contour))
                frac = area / float(w * h)
                aspect = bw / float(max(bh, 1))
                if frac < 0.0008 or frac > 0.05 or aspect < 0.5 or aspect > 8.0:
                    continue
                blobs.append((area, x + bw / 2.0, y + bh / 2.0))
            blobs.sort(reverse=True)
            for area, cx, cy in blobs[: self.max_n]:
                out.append(Detection(alliance, cx / w, cy / w, min(1.0, area / (0.01 * w * h))))
        return out


class YoloDetector:
    """Ultralytics YOLOv8 ile robot/bumper tespiti."""

    def __init__(self, weights: str, cv2, np, conf: float = 0.35, imgsz: int = 640) -> None:
        if not os.path.isfile(weights):
            raise AnalyzerError(f"YOLO ağırlık dosyası bulunamadı: {weights}")
        try:
            from ultralytics import YOLO  # noqa: WPS433
        except ImportError as exc:
            raise AnalyzerError("ultralytics kurulu değil: pip install ultralytics") from exc
        self.cv2, self.np = cv2, np
        self.model = YOLO(weights)
        self.names = getattr(self.model, "names", {}) or {}
        self.conf, self.imgsz = conf, imgsz
        self.name = f"yolo:{Path(weights).name}"

    def _alliance_from_color(self, frame, x1: int, y1: int, x2: int, y2: int) -> Optional[str]:
        cv2, np = self.cv2, self.np
        top = y1 + int((y2 - y1) * 0.6)  # bumper kutunun alt kısmındadır
        crop = frame[max(0, top):max(0, y2), max(0, x1):max(0, x2)]
        if crop.size == 0:
            return None
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        masks = color_masks(cv2, np, hsv)
        red, blue = int(masks["red"].sum() // 255), int(masks["blue"].sum() // 255)
        total = crop.shape[0] * crop.shape[1]
        if max(red, blue) < 0.05 * total:
            return None
        return "red" if red >= blue else "blue"

    def detect(self, frame) -> List[Detection]:
        result = self.model.predict(frame, conf=self.conf, imgsz=self.imgsz, verbose=False)[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        h, w = frame.shape[:2]
        xyxy = boxes.xyxy.cpu().numpy()
        cls = boxes.cls.cpu().numpy().astype(int)
        confs = boxes.conf.cpu().numpy()
        out: List[Detection] = []
        for (x1, y1, x2, y2), c, score in zip(xyxy, cls, confs):
            label = str(self.names.get(int(c), "")).lower()
            if "red" in label:
                alliance: Optional[str] = "red"
            elif "blue" in label:
                alliance = "blue"
            else:
                alliance = self._alliance_from_color(frame, int(x1), int(y1), int(x2), int(y2))
            if alliance is None:
                continue
            out.append(Detection(alliance, float((x1 + x2) / 2.0 / w), float((y1 + y2) / 2.0 / w), float(score)))
        return out


# ---------------------------------------------------------------------------
# Takip
# ---------------------------------------------------------------------------

class Track:
    __slots__ = ("tid", "alliance", "pts", "last_t", "last_xy")

    def __init__(self, tid: int, alliance: str) -> None:
        self.tid = tid
        self.alliance = alliance
        self.pts: Pts = {}
        self.last_t = -1e9
        self.last_xy: Point = (0.0, 0.0)

    def add(self, t: float, x: float, y: float) -> None:
        self.pts[t] = (x, y)
        self.last_t = t
        self.last_xy = (x, y)


class Tracker:
    """Her ittifak için açgözlü en-yakın-komşu eşleştirmeli basit çoklu nesne takipçisi."""

    def __init__(self, p: Params) -> None:
        self.p = p
        self.tracks: List[Track] = []
        self._active: Dict[str, List[Track]] = {"red": [], "blue": []}
        self._next_id = 1

    def update(self, t: float, detections: Sequence[Detection]) -> None:
        p = self.p
        for alliance in ("red", "blue"):
            dets = [d for d in detections if d.alliance == alliance]
            active = [tr for tr in self._active[alliance] if t - tr.last_t <= p.track_lost_s]
            pairs = []
            for di, d in enumerate(dets):
                for ti, tr in enumerate(active):
                    pairs.append((math.hypot(d.cx - tr.last_xy[0], d.cy - tr.last_xy[1]), di, ti))
            pairs.sort()
            used_d: Set[int] = set()
            used_t: Set[int] = set()
            for dist, di, ti in pairs:
                if di in used_d or ti in used_t:
                    continue
                gap_samples = max(1.0, (t - active[ti].last_t) * p.sample_fps)
                if dist > p.track_max_jump * gap_samples:
                    continue
                active[ti].add(t, dets[di].cx, dets[di].cy)
                used_d.add(di)
                used_t.add(ti)
            for di, d in enumerate(dets):
                if di in used_d:
                    continue
                track = Track(self._next_id, alliance)
                self._next_id += 1
                track.add(t, d.cx, d.cy)
                self.tracks.append(track)
                active.append(track)
            self._active[alliance] = active


# ---------------------------------------------------------------------------
# Hareket analizi (saf fonksiyonlar; sentetik veriyle test edilebilir)
# ---------------------------------------------------------------------------

SpeedEntry = Tuple[float, float, float]  # (t, hız, geriye bakış zamanı t0)


def speed_series(pts: Pts, window: float = 1.0) -> List[SpeedEntry]:
    """Her nokta için son `window` sn içindeki yer değiştirmeden hız (genişlik/sn)."""
    ts = sorted(pts)
    out: List[SpeedEntry] = []
    m = 0
    for j, t in enumerate(ts):
        if ts[0] > t - window + 1e-9:
            continue
        while m + 1 < j and ts[m + 1] <= t - window + 1e-9:
            m += 1
        t0 = ts[m]
        if t - t0 > window * 2.5:  # iz kopmuş
            continue
        (x0, y0), (x1, y1) = pts[t0], pts[t]
        out.append((t, math.hypot(x1 - x0, y1 - y0) / (t - t0), t0))
    return out


@dataclass
class Segment:
    start: float
    end: float
    x: float
    y: float

    @property
    def dur(self) -> float:
        return self.end - self.start


def stationary_segments(series: Sequence[SpeedEntry], pts: Pts, p: Params) -> List[Segment]:
    segments: List[Segment] = []
    cur: Optional[List] = None  # [start, end, last_t]
    for t, v, t0 in series:
        still = v < p.still_speed
        if still and cur is not None and t - cur[2] <= 1.0:
            cur[1] = t
            cur[2] = t
        else:
            if cur is not None:
                segments.append(_finish_segment(cur, pts))
                cur = None
            if still:
                cur = [t0, t, t]
    if cur is not None:
        segments.append(_finish_segment(cur, pts))
    return segments


def _finish_segment(cur: List, pts: Pts) -> Segment:
    inside = [pts[t] for t in pts if cur[0] <= t <= cur[1]]
    if not inside:
        return Segment(cur[0], cur[1], 0.0, 0.0)
    return Segment(cur[0], cur[1], statistics.fmean(x for x, _ in inside), statistics.fmean(y for _, y in inside))


def _traveled_away(pts: Pts, lo: float, hi: float, cx: float, cy: float, min_dist: float) -> bool:
    """[lo, hi] arasında robot, (cx, cy) noktasından en az min_dist uzaklaştı mı?"""
    return any(math.hypot(x - cx, y - cy) >= min_dist for t, (x, y) in pts.items() if lo <= t <= hi)


def find_cycles(segments: Sequence[Segment], pts: Pts, p: Params) -> Tuple[List[Tuple[float, float]], Optional[dict]]:
    """En çok ziyaret edilen durak noktasına art arda varışlar arası süre = cycle.

    İki ziyaret arasında robot durak noktasından gerçekten uzaklaşmış olmalıdır; aksi halde
    aynı duruşun gürültüyle bölünmesi sahte cycle üretirdi."""
    clusters: List[dict] = []
    for seg in segments:
        if seg.dur < p.dwell_min_s:
            continue
        for c in clusters:
            if math.hypot(seg.x - c["x"], seg.y - c["y"]) <= p.cluster_radius:
                c["n"] += 1
                c["x"] += (seg.x - c["x"]) / c["n"]
                c["y"] += (seg.y - c["y"]) / c["n"]
                c["visits"].append(seg.start)
                break
        else:
            clusters.append({"x": seg.x, "y": seg.y, "n": 1, "visits": [seg.start]})
    if not clusters:
        return [], None
    primary = max(clusters, key=lambda c: c["n"])
    if primary["n"] < 2:
        return [], None
    visits = sorted(primary["visits"])
    cycles = [
        (a, b) for a, b in zip(visits, visits[1:])
        if p.cycle_min_s <= (b - a) <= p.cycle_max_s
        and _traveled_away(pts, a, b, primary["x"], primary["y"], p.cluster_radius * 2)
    ]
    return cycles, primary


def find_downtime(segments: Sequence[Segment], primary: Optional[dict], p: Params) -> List[Tuple[float, float]]:
    """Uzun süre duran ve skor/durak noktasında olmayan aralıklar (veya çok uzun duruşlar)."""
    events: List[Tuple[float, float]] = []
    for seg in segments:
        if seg.dur < p.downtime_min_s:
            continue
        at_station = primary is not None and primary["n"] >= 2 and \
            math.hypot(seg.x - primary["x"], seg.y - primary["y"]) <= p.cluster_radius
        if at_station and seg.dur < 2 * p.downtime_min_s:
            continue
        events.append((seg.start, seg.end))
    return events


def find_defense_windows(pts_a: Pts, pts_b: Pts, series_a: Sequence[SpeedEntry], series_b: Sequence[SpeedEntry],
                         p: Params) -> List[Tuple[float, float, str]]:
    """İki karşı ittifak robotunun uzun süre yakın kaldığı aralıklar. Savunmacı: yaklaşan (hızlı) robot."""
    common = sorted(set(pts_a) & set(pts_b))
    windows: List[Tuple[float, float]] = []
    start: Optional[float] = None
    last = 0.0
    for t in common:
        (xa, ya), (xb, yb) = pts_a[t], pts_b[t]
        if math.hypot(xa - xb, ya - yb) > p.defense_dist:
            continue
        if start is None:
            start = last = t
        elif t - last <= 1.0:
            last = t
        else:
            windows.append((start, last))
            start = last = t
    if start is not None:
        windows.append((start, last))

    def mean_speed(series: Sequence[SpeedEntry], lo: float, hi: float) -> float:
        vals = [v for t, v, _ in series if lo <= t <= hi]
        return statistics.fmean(vals) if vals else 0.0

    out: List[Tuple[float, float, str]] = []
    for lo, hi in windows:
        if hi - lo < p.defense_min_s:
            continue
        if max(mean_speed(series_a, lo, hi), mean_speed(series_b, lo, hi)) < p.still_speed * 1.5:
            continue  # ikisi de duruyor: savunma değil, yan yana bekleme
        approach_a = mean_speed(series_a, lo - 1.5, lo)
        approach_b = mean_speed(series_b, lo - 1.5, lo)
        out.append((lo, hi, "a" if approach_a >= approach_b else "b"))
    return out


def detect_match_start(tracks: Sequence[Track], p: Params) -> float:
    """En az 2 robotun art arda 2 saniye hareketli olduğu ilk an = maç başlangıcı."""
    moving: Dict[int, Set[int]] = {}
    earliest: Optional[float] = None
    for idx, track in enumerate(tracks):
        for t, v, _ in speed_series(track.pts, p.speed_window_s):
            if earliest is None or t < earliest:
                earliest = t
            if v >= p.move_speed:
                moving.setdefault(int(t), set()).add(idx)
    for sec in sorted(moving):
        if len(moving[sec]) >= 2 and len(moving.get(sec + 1, ())) >= 2:
            # Hız 1 sn'lik pencereden hesaplandığı için hareket yaklaşık bu kadar önce başlamıştır.
            return max(float(earliest or 0.0), float(sec) - p.speed_window_s)
    if earliest is not None:
        return float(earliest)
    all_t = [t for tr in tracks for t in tr.pts]
    return float(min(all_t)) if all_t else 0.0


def pick_tracks(tracks: Sequence[Track], alliance: str, t_start: float, t_end: float, p: Params, count: int) -> List[Track]:
    """İttifak için en uzun `count` izi seçer ve üstten alta (başlangıç y konumu) sıralar."""
    need = max(1, int(p.min_track_s * p.sample_fps))
    candidates = []
    for tr in tracks:
        if tr.alliance != alliance:
            continue
        inside = sorted(t for t in tr.pts if t_start <= t <= t_end)
        if len(inside) >= need:
            candidates.append((len(inside), tr, inside))
    candidates.sort(key=lambda c: -c[0])
    chosen = candidates[:count]

    def start_y(item) -> float:
        _, tr, inside = item
        return statistics.median(tr.pts[t][1] for t in inside[:10])

    chosen.sort(key=start_y)
    return [tr for _, tr, _ in chosen]


def path_length(pts: Pts, lo: float, hi: float) -> float:
    ts = sorted(t for t in pts if lo <= t <= hi)
    return sum(math.hypot(pts[b][0] - pts[a][0], pts[b][1] - pts[a][1]) for a, b in zip(ts, ts[1:]) if b - a <= 1.5)


def analyze_tracks(match: dict, tracks: Sequence[Track], duration_s: float, p: Params, meta: dict) -> dict:
    """Takip izlerinden maç JSON'unu üretir."""
    t_start = detect_match_start(tracks, p)
    t_auto_end = t_start + p.auto_s
    t_end = t_start + p.match_s
    window_len = max(1.0, min(t_end, duration_s if duration_s > 0 else t_end) - t_start)
    expected_samples = window_len * p.sample_fps

    teams: Dict[str, dict] = {}
    timeline: List[dict] = []
    clipped: Dict[str, Pts] = {}
    series_by_team: Dict[str, List[SpeedEntry]] = {}
    side_of: Dict[str, str] = {}
    defense_by_team: Dict[str, List[Tuple[float, float]]] = {}

    for side in ("red", "blue"):
        keys = match["alliances"][side]["team_keys"]
        chosen = pick_tracks(tracks, side, t_start, t_end, p, len(keys))
        for pos, key in enumerate(keys):
            side_of[key] = side
            entry = {"alliance": side, "station": pos + 1, "track_coverage": 0.0,
                     "auto": None, "cycles": None, "defense": None, "downtime": None}
            teams[key] = entry
            if pos >= len(chosen):
                continue
            pts = {t: xy for t, xy in chosen[pos].pts.items() if t_start <= t <= t_end}
            series = speed_series(pts, p.speed_window_s)
            segments = stationary_segments(series, pts, p)
            cycles, primary = find_cycles(segments, pts, p)
            downs = find_downtime(segments, primary, p)

            clipped[key] = pts
            series_by_team[key] = series
            defense_by_team[key] = []

            entry["track_coverage"] = rnd(min(1.0, len(pts) / expected_samples))
            moved_len = path_length(pts, t_start, t_auto_end)
            active = sum(1 for t, v, _ in series if t_start <= t <= t_auto_end and v >= p.move_speed) / p.sample_fps
            entry["auto"] = {"moved": moved_len >= p.auto_move_min, "active_s": rnd(active)}
            if entry["auto"]["moved"]:
                timeline.append({"t": rnd(t_start), "end": rnd(t_auto_end), "type": "auto", "team": key, "label": "Otonom hareket"})

            durations = [b - a for a, b in cycles]
            entry["cycles"] = {
                "count": len(cycles),
                "avg_s": rnd(statistics.fmean(durations)) if durations else None,
                "best_s": rnd(min(durations)) if durations else None,
                "times": [{"start_s": rnd(a), "end_s": rnd(b), "duration_s": rnd(b - a)} for a, b in cycles],
            }
            for i, (a, b) in enumerate(cycles, 1):
                timeline.append({"t": rnd(a), "end": rnd(b), "type": "cycle", "team": key, "label": f"Cycle {i} · {b - a:.1f} sn"})

            entry["downtime"] = {"total_s": rnd(sum(b - a for a, b in downs)),
                                 "events": [{"start_s": rnd(a), "end_s": rnd(b)} for a, b in downs]}
            for a, b in downs:
                timeline.append({"t": rnd(a), "end": rnd(b), "type": "downtime", "team": key, "label": "Hareketsiz"})

    for red_key in [k for k, s in side_of.items() if s == "red" and k in clipped]:
        for blue_key in [k for k, s in side_of.items() if s == "blue" and k in clipped]:
            for lo, hi, who in find_defense_windows(clipped[red_key], clipped[blue_key],
                                                    series_by_team[red_key], series_by_team[blue_key], p):
                defender = red_key if who == "a" else blue_key
                defense_by_team[defender].append((lo, hi))

    for key, events in defense_by_team.items():
        teams[key]["defense"] = {"total_s": rnd(sum(b - a for a, b in events)),
                                 "events": [{"start_s": rnd(a), "end_s": rnd(b)} for a, b in events]}
        for a, b in events:
            timeline.append({"t": rnd(a), "end": rnd(b), "type": "defense", "team": key, "label": "Savunma"})

    timeline.sort(key=lambda e: (e["t"], e["team"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "match_key": match["key"],
        "event_key": match["event_key"],
        "analyzer_version": ANALYZER_VERSION,
        "generated_at": now_iso(),
        "detector": meta.get("detector", ""),
        "identity_method": "station_order_heuristic",
        "video": {"youtube_id": meta.get("youtube_id", ""), "duration_s": rnd(duration_s), "sample_fps": p.sample_fps},
        "periods": {"auto_start_s": rnd(t_start), "auto_end_s": rnd(t_auto_end), "teleop_end_s": rnd(t_end)},
        "teams": teams,
        "timeline": timeline,
    }


# ---------------------------------------------------------------------------
# Video indirme ve kare işleme
# ---------------------------------------------------------------------------

def download_video(video_id: str, workdir: Path, p: Params) -> Path:
    if not YOUTUBE_ID_RE.match(video_id):
        raise AnalyzerError(f"Geçersiz YouTube kimliği: {video_id!r}")
    try:
        import yt_dlp  # noqa: WPS433
    except ImportError as exc:
        raise AnalyzerError("yt-dlp kurulu değil: pip install yt-dlp") from exc
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        "format": "best[height<=480][ext=mp4]/best[height<=480]/worst[ext=mp4]/worst",
        "outtmpl": str(workdir / f"{video_id}.%(ext)s"),
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = (info or {}).get("duration")
            if duration and duration > p.max_video_s:
                raise SkipVideo(f"Video {int(duration)} sn (uzun yayın); {int(p.max_video_s)} sn sınırı nedeniyle atlandı.")
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except SkipVideo:
        raise
    except Exception as exc:  # yt_dlp.utils.DownloadError dahil
        raise AnalyzerError(f"Video indirilemedi ({video_id}): {exc}") from exc
    if not path.is_file():
        matches = sorted(workdir.glob(f"{video_id}.*"))
        if not matches:
            raise AnalyzerError(f"İndirilen dosya bulunamadı ({video_id}).")
        path = matches[0]
    return path


def run_detection(video_path: Path, detector, cv2, p: Params) -> Tuple[List[Track], float, int]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise AnalyzerError(f"Video açılamadı: {video_path}")
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or math.isnan(fps) or fps <= 0:
            fps = 30.0
        step = max(1, int(round(fps / p.sample_fps)))
        tracker = Tracker(p)
        idx = 0
        sampled = 0
        while True:
            if not cap.grab():
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    if w != p.frame_width and w > 0:
                        frame = cv2.resize(frame, (p.frame_width, max(1, int(h * p.frame_width / w))))
                    tracker.update(round(idx / fps, 3), detector.detect(frame))
                    sampled += 1
            idx += 1
        return tracker.tracks, idx / fps, sampled
    finally:
        cap.release()


def make_detector(weights: Optional[str], conf: float):
    try:
        import cv2  # noqa: WPS433
        import numpy as np  # noqa: WPS433
    except ImportError as exc:
        raise AnalyzerError("opencv-python / numpy kurulu değil: pip install opencv-python numpy") from exc
    if weights:
        detector = YoloDetector(weights, cv2, np, conf=conf)
    else:
        log.warning("--weights verilmedi: yalnızca HSV renk yedeği kullanılıyor (düşük doğruluk).")
        detector = ColorDetector(cv2, np)
    return detector, cv2


def process_video(video_path: Path, match: dict, detector, cv2, p: Params) -> dict:
    tracks, duration, sampled = run_detection(video_path, detector, cv2, p)
    if sampled == 0 or not tracks:
        raise AnalyzerError("Videoda hiç robot/bumper tespit edilemedi.")
    meta = {"detector": getattr(detector, "name", "unknown"), "youtube_id": match.get("youtube_id", "")}
    return analyze_tracks(match, tracks, duration, p, meta)


# ---------------------------------------------------------------------------
# Komutlar
# ---------------------------------------------------------------------------

class Runner:
    def __init__(self, args: argparse.Namespace, p: Params) -> None:
        self.args, self.p = args, p
        self.client = TbaClient(args.api_key or os.environ.get("TBA_API_KEY", ""))
        self._detector = None
        self._cv2 = None
        self.stats = {"done": 0, "skipped": 0, "failed": 0, "no_video": 0}
        self.failures: List[str] = []

    def _get_detector(self):
        if self._detector is None:
            self._detector, self._cv2 = make_detector(self.args.weights, self.args.conf)
            log.info("Dedektör: %s", getattr(self._detector, "name", "?"))
        return self._detector, self._cv2

    def analyze_match(self, year: int, match: dict, video_file: Optional[Path] = None) -> None:
        key = match["key"]
        out = output_path(year, match["event_key"], key)
        if out.exists() and not self.args.force:
            log.info("[atla] %s zaten analiz edilmiş (--force ile yeniden).", key)
            self.stats["skipped"] += 1
            return
        if not video_file and not match["youtube_id"]:
            log.info("[atla] %s için TBA'da YouTube videosu yok.", key)
            self.stats["no_video"] += 1
            return
        if self.args.dry_run:
            log.info("[kuru çalıştırma] %s -> %s (video: %s)", key, out, match["youtube_id"] or video_file)
            return
        workdir = Path(tempfile.mkdtemp(prefix="octo-"))
        try:
            detector, cv2 = self._get_detector()
            path = video_file or download_video(match["youtube_id"], workdir, self.p)
            log.info("[analiz] %s (%s)…", key, path.name)
            started = time.time()
            result = process_video(path, match, detector, cv2, self.p)
            atomic_write_json(out, result)
            log.info("[tamam] %s -> %s (%.0f sn)", key, out, time.time() - started)
            self.stats["done"] += 1
        except SkipVideo as exc:
            log.warning("[atla] %s: %s", key, exc)
            self.stats["skipped"] += 1
        except AnalyzerError as exc:
            log.error("[hata] %s: %s", key, exc)
            self.stats["failed"] += 1
            self.failures.append(f"{key}: {exc}")
        except Exception as exc:  # tek maç hatası toplu işlemi durdurmasın
            log.exception("[hata] %s: beklenmeyen hata", key)
            self.stats["failed"] += 1
            self.failures.append(f"{key}: {exc}")
        finally:
            if self.args.keep_video and not video_file:
                log.info("Geçici klasör korundu: %s", workdir)
            else:
                shutil.rmtree(workdir, ignore_errors=True)

    def analyze_event(self, year: int, event_key: str, only_match: Optional[str] = None) -> None:
        matches = fetch_event_matches(self.client, event_key)
        if only_match:
            matches = [m for m in matches if m["key"] == only_match]
            if not matches:
                raise AnalyzerError(f"Maç bulunamadı: {only_match}")
        if self.args.limit:
            matches = matches[: self.args.limit]
        log.info("%s: %d maç (%d videolu)", event_key, len(matches), sum(1 for m in matches if m["youtube_id"]))
        for match in matches:
            self.analyze_match(year, match)


def cmd_events(args: argparse.Namespace) -> int:
    client = TbaClient(args.api_key or os.environ.get("TBA_API_KEY", ""))
    events = list_turkey_events(client, args.year)
    if not events:
        print(f"{args.year} için Türkiye etkinliği bulunamadı.")
        return 0
    for e in events:
        kind = e.get("event_type_string") or e.get("event_type")
        print(f"{e['key']:<14} {e.get('start_date', '')}  {kind!s:<22} {e.get('name', '')} · {e.get('city', '')}")
    return 0


def year_of_key(key: str) -> int:
    return int(key[:4])


def cmd_analyze(args: argparse.Namespace, p: Params) -> int:
    if not args.event and not args.match:
        raise AnalyzerError("--event veya --match belirtin.")
    runner = Runner(args, p)
    if args.match:
        if not MATCH_KEY_RE.match(args.match):
            raise AnalyzerError(f"Geçersiz maç anahtarı: {args.match}")
        event_key = args.match.split("_", 1)[0]
        year = year_of_key(event_key)
        if args.video_file:
            raw = runner.client.get(f"/match/{args.match}")
            if raw is None:
                raise AnalyzerError(f"Maç TBA'da bulunamadı: {args.match}")
            runner.analyze_match(year, simplify_match(raw), Path(args.video_file))
        else:
            runner.analyze_event(year, event_key, only_match=args.match)
    else:
        if not EVENT_KEY_RE.match(args.event):
            raise AnalyzerError(f"Geçersiz etkinlik anahtarı: {args.event}")
        runner.analyze_event(year_of_key(args.event), args.event)
    return report(runner)


def cmd_all(args: argparse.Namespace, p: Params) -> int:
    runner = Runner(args, p)
    events = list_turkey_events(runner.client, args.year)
    log.info("%d Türkiye etkinliği bulundu.", len(events))
    for event in events:
        try:
            runner.analyze_event(args.year, event["key"])
        except AnalyzerError as exc:
            log.error("[hata] %s: %s", event["key"], exc)
            runner.stats["failed"] += 1
            runner.failures.append(f"{event['key']}: {exc}")
    return report(runner)


def report(runner: Runner) -> int:
    s = runner.stats
    print(f"\nÖzet: {s['done']} tamamlandı · {s['skipped']} atlandı · {s['no_video']} videosuz · {s['failed']} hatalı")
    for line in runner.failures:
        print("  ✗", line)
    return 1 if s["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Octo-Replay yerel analiz scripti", formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="ayrıntılı günlük")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--api-key", default="", help="TBA API anahtarı (varsayılan: TBA_API_KEY ortam değişkeni)")

    ev = sub.add_parser("events", parents=[common], help="Türkiye etkinliklerini listele")
    ev.add_argument("--year", type=int, default=2026)

    work = argparse.ArgumentParser(add_help=False)
    work.add_argument("--weights", default="", help="özel YOLOv8 bumper ağırlığı (.pt); yoksa HSV yedeği")
    work.add_argument("--conf", type=float, default=0.35, help="YOLO güven eşiği")
    work.add_argument("--force", action="store_true", help="mevcut JSON'ların üzerine yaz")
    work.add_argument("--limit", type=int, default=0, help="etkinlik başına en çok N maç")
    work.add_argument("--dry-run", action="store_true", help="indirmeden/analiz etmeden yapılacakları listele")
    work.add_argument("--keep-video", action="store_true", help="indirilen videoyu silme")
    work.add_argument("--max-video-s", type=float, default=Params.max_video_s, help="bundan uzun videoları atla")

    an = sub.add_parser("analyze", parents=[common, work], help="bir etkinliği veya tek maçı analiz et")
    an.add_argument("--event", default="", help="etkinlik anahtarı, örn. 2026tuis")
    an.add_argument("--match", default="", help="maç anahtarı, örn. 2026tuis_qm12")
    an.add_argument("--video-file", default="", help="indirmek yerine yerel video dosyası kullan (--match ile)")

    al = sub.add_parser("all", parents=[common, work], help="yıldaki tüm Türkiye etkinliklerini analiz et")
    al.add_argument("--year", type=int, default=2026)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    try:
        if args.command == "events":
            return cmd_events(args)
        p = Params(max_video_s=args.max_video_s)
        if args.command == "analyze":
            return cmd_analyze(args, p)
        return cmd_all(args, p)
    except AnalyzerError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("Kullanıcı tarafından durduruldu.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
