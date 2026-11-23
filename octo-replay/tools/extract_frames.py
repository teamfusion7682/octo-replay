#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Octo-Replay · eğitim verisi için maç videolarından kare çıkarır (yerel çalıştırılır).

Maç videolarını indirir (yt-dlp) ve belirli aralıkla JPG kare çıkarır. Çıktı:
    dataset/videos/{video_id}.mp4
    dataset/frames/{video_id}_f000123.jpg      <- etiketleme aracına bu klasörü yükleyin

Kare adındaki video kimliği, `split_dataset.py`'nin kareleri MAÇA göre ayırmasını sağlar;
dosyaları yeniden adlandırmayın.

Örnekler (proje klasöründe):
  python tools/extract_frames.py --all-turkey --year 2026 --every 3
  python tools/extract_frames.py --event 2026tuis --videos 8 --every 2
  python tools/extract_frames.py --match 2026tuis_qm12 --match 2026tuis_qm40
  python tools/extract_frames.py --url https://www.youtube.com/watch?v=XXXXXXXXXXX
  python tools/extract_frames.py --video-file maclar/ornek.mp4

TBA kullanan seçenekler (--event / --match) için TBA_API_KEY ortam değişkeni gerekir.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyzer as A  # noqa: E402  (aynı projedeki analiz modülü; ağır bağımlılıkları gecikmeli içe aktarır)

YOUTUBE_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|^)([\w-]{11})(?:[&?#/]|$)")


def video_id_from(text: str) -> str:
    m = YOUTUBE_RE.search(text.strip())
    if not m:
        raise A.AnalyzerError(f"YouTube kimliği çıkarılamadı: {text!r}")
    return m.group(1)


def safe_tag(text: str) -> str:
    return re.sub(r"[^\w-]+", "-", text).strip("-") or "video"


def spread(items: List, count: int) -> List:
    """Listeden eşit aralıklı `count` öğe seçer (etkinliğin başından sonuna çeşitlilik için)."""
    if count <= 0 or count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def extract_frames(video_path: Path, out_dir: Path, tag: str, every_s: float, max_frames: int,
                   max_width: int, quality: int) -> int:
    try:
        import cv2  # noqa: WPS433
    except ImportError as exc:
        raise A.AnalyzerError("opencv-python kurulu değil: pip install opencv-python") from exc
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise A.AnalyzerError(f"Video açılamadı: {video_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps != fps or fps <= 0:
            fps = 30.0
        step = max(1, int(round(every_s * fps)))
        idx = saved = 0
        while True:
            if not cap.grab():
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    h, w = frame.shape[:2]
                    if w > max_width:
                        frame = cv2.resize(frame, (max_width, max(1, int(h * max_width / w))))
                    if float(frame.std()) >= 8.0:  # tamamen siyah/düz kareleri atla
                        target = out_dir / f"{tag}_f{idx:06d}.jpg"
                        if cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
                            saved += 1
                            if max_frames and saved >= max_frames:
                                break
            idx += 1
        return saved
    finally:
        cap.release()


def collect_sources(args: argparse.Namespace) -> List[Tuple[str, Optional[str], Optional[Path]]]:
    """(etiket, youtube_id, yerel_dosya) listesi döndürür."""
    sources: List[Tuple[str, Optional[str], Optional[Path]]] = []
    seen = set()

    def add_video(vid: str) -> None:
        if vid not in seen:
            seen.add(vid)
            sources.append((vid, vid, None))

    for url in args.url or []:
        add_video(video_id_from(url))
    for path in args.video_file or []:
        p = Path(path)
        if not p.is_file():
            raise A.AnalyzerError(f"Dosya bulunamadı: {p}")
        sources.append((safe_tag(p.stem), None, p))

    if args.match or args.event or args.all_turkey:
        client = A.TbaClient(args.api_key or __import__("os").environ.get("TBA_API_KEY", ""))
        for key in args.match or []:
            if not A.MATCH_KEY_RE.match(key):
                raise A.AnalyzerError(f"Geçersiz maç anahtarı: {key}")
            raw = client.get(f"/match/{key}")
            vid = A.first_youtube_id(raw) if isinstance(raw, dict) else ""
            if vid:
                add_video(vid)
            else:
                print(f"[atla] {key}: TBA'da YouTube videosu yok")
        event_keys: List[str] = []
        if args.event:
            if not A.EVENT_KEY_RE.match(args.event):
                raise A.AnalyzerError(f"Geçersiz etkinlik anahtarı: {args.event}")
            event_keys.append(args.event)
        if args.all_turkey:
            turkey = A.list_turkey_events(client, args.year)
            print(f"{args.year}: {len(turkey)} Türkiye etkinliği bulundu (off-season dahil).")
            event_keys.extend(e["key"] for e in turkey if e["key"] not in event_keys)
        # --event için varsayılan 8 video; --all-turkey için varsayılan: hepsi
        per_event = args.videos if args.videos is not None else (0 if args.all_turkey else 8)
        for key in event_keys:
            try:
                matches = [m for m in A.fetch_event_matches(client, key) if m["youtube_id"]]
            except A.AnalyzerError as exc:  # tek etkinlik hatası diğerlerini durdurmasın
                print(f"[atla] {key}: {exc}")
                continue
            ids: List[str] = []
            for m in matches:
                if m["youtube_id"] not in ids:
                    ids.append(m["youtube_id"])
            chosen = spread(ids, per_event)
            print(f"{key}: {len(ids)} farklı maç videosu, {len(chosen)} tanesi işlenecek.")
            for vid in chosen:
                add_video(vid)
    return sources


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Maç videolarından eğitim karesi çıkar", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--event", default="", help="etkinlik anahtarı (örn. 2026tuis); videolar etkinliğe yayılarak seçilir")
    ap.add_argument("--all-turkey", action="store_true", help="--year'daki TÜM Türkiye etkinliklerini (off-season dahil) işle; varsayılan: tüm videolar")
    ap.add_argument("--year", type=int, default=2026, help="--all-turkey için sezon (varsayılan 2026)")
    ap.add_argument("--videos", type=int, default=None, help="etkinlik başına kaç video (0 = hepsi). Varsayılan: --event ile 8, --all-turkey ile hepsi")
    ap.add_argument("--match", action="append", help="maç anahtarı (birden çok kez verilebilir)")
    ap.add_argument("--url", action="append", help="YouTube adresi veya kimliği (birden çok kez)")
    ap.add_argument("--video-file", action="append", help="yerel video dosyası (birden çok kez)")
    ap.add_argument("--every", type=float, default=2.0, help="kaç saniyede bir kare (varsayılan 2)")
    ap.add_argument("--max-frames", type=int, default=0, help="video başına en çok kare (0 = sınırsız)")
    ap.add_argument("--max-width", type=int, default=1280, help="kare genişliği bundan büyükse küçült")
    ap.add_argument("--quality", type=int, default=92, help="JPG kalitesi")
    ap.add_argument("--max-video-s", type=float, default=900.0, help="bundan uzun videoları (tam yayın) atla")
    ap.add_argument("--out", default="dataset", help="çıktı klasörü (varsayılan: dataset)")
    ap.add_argument("--api-key", default="", help="TBA anahtarı (varsayılan: TBA_API_KEY ortam değişkeni)")
    args = ap.parse_args(argv)

    if not (args.event or args.match or args.url or args.video_file or args.all_turkey):
        ap.error("--all-turkey, --event, --match, --url veya --video-file belirtin.")
    if args.every <= 0:
        ap.error("--every 0'dan büyük olmalı.")

    out = Path(args.out)
    videos_dir, frames_dir = out / "videos", out / "frames"
    videos_dir.mkdir(parents=True, exist_ok=True)
    params = A.Params(max_video_s=args.max_video_s)
    total = failed = 0
    try:
        sources = collect_sources(args)
    except A.AnalyzerError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 2
    if not sources:
        print("İşlenecek video bulunamadı.")
        return 1

    for tag, vid, local in sources:
        try:
            if local is None:
                existing = sorted(videos_dir.glob(f"{vid}.*"))
                existing = [p for p in existing if p.suffix not in (".part", ".ytdl")]
                path = existing[0] if existing else A.download_video(vid, videos_dir, params)
            else:
                path = local
            count = extract_frames(path, frames_dir, tag, args.every, args.max_frames, args.max_width, args.quality)
            print(f"[tamam] {tag}: {count} kare -> {frames_dir}")
            total += count
        except A.SkipVideo as exc:
            print(f"[atla] {tag}: {exc}")
        except A.AnalyzerError as exc:
            print(f"[hata] {tag}: {exc}", file=sys.stderr)
            failed += 1
    print(f"\nToplam {total} kare. Sıradaki adım: {frames_dir} klasörünü etiketleme aracına (Roboflow, CVAT…) yükleyin.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
