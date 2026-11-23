#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Octo-Replay · etiketlenmiş YOLO verisini eğitim/doğrulama olarak MAÇA göre böler ve data.yaml üretir.

Karelere göre rastgele bölmek hatalıdır: aynı maçın komşu kareleri hem eğitimde hem doğrulamada
olur ve model olduğundan iyi görünür. Bu script bir videonun (maçın) tüm karelerini aynı tarafa koyar.
Grup, dosya adının başındaki `{video_id}_f000123` kalıbından bulunur (extract_frames.py bu adı verir;
Roboflow gibi araçlar bu öneki korur).

Kullanım:
  python tools/split_dataset.py --images export/images --labels export/labels
  python tools/split_dataset.py --images export/images --labels export/labels --val-ratio 0.2 --classes red_bumper blue_bumper

Sonra:
  yolo detect train data=dataset_yolo/data.yaml model=yolov8n.pt imgsz=640 epochs=100 batch=16 device=0
"""
from __future__ import annotations

import argparse
import random
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

IMAGE_EXT = {".jpg", ".jpeg", ".png"}
GROUP_RE = re.compile(r"^(?P<group>.+?)_f\d{6}")


def group_of(stem: str) -> str:
    m = GROUP_RE.match(stem)
    return m.group("group") if m else stem


def split_groups(groups: Dict[str, List[Path]], val_ratio: float, seed: int) -> Dict[str, str]:
    """Her grubu 'train' veya 'val' olarak atar; her iki tarafta da en az bir grup kalır."""
    names = sorted(groups)
    random.Random(seed).shuffle(names)
    total = sum(len(v) for v in groups.values())
    target = total * val_ratio
    assignment: Dict[str, str] = {}
    val_count = 0
    for i, name in enumerate(names):
        remaining_after = len(names) - i - 1
        need_val = val_count == 0 and remaining_after >= 1  # ilk grup doğrulamaya girsin (train'e en az 1 grup kalmalı)
        if val_count < target or need_val:
            if remaining_after == 0 and "train" not in assignment.values():
                assignment[name] = "train"
            else:
                assignment[name] = "val"
                val_count += len(groups[name])
        else:
            assignment[name] = "train"
    return assignment


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="YOLO verisini maça göre böl", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--images", required=True, help="etiketlenmiş görüntülerin klasörü")
    ap.add_argument("--labels", required=True, help="YOLO .txt etiketlerinin klasörü")
    ap.add_argument("--out", default="dataset_yolo", help="çıktı klasörü (varsayılan: dataset_yolo)")
    ap.add_argument("--val-ratio", type=float, default=0.2, help="doğrulama oranı (varsayılan 0.2)")
    ap.add_argument("--classes", nargs="+", default=["red_bumper", "blue_bumper"], help="sınıf adları, etiketleme sırasıyla")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    images_dir, labels_dir, out = Path(args.images), Path(args.labels), Path(args.out)
    if not images_dir.is_dir() or not labels_dir.is_dir():
        print("Hata: --images ve --labels mevcut klasörler olmalı.", file=sys.stderr)
        return 2
    if not 0.05 <= args.val_ratio <= 0.5:
        print("Hata: --val-ratio 0.05 ile 0.5 arasında olmalı.", file=sys.stderr)
        return 2

    groups: Dict[str, List[Path]] = {}
    for p in sorted(images_dir.iterdir()):
        if p.suffix.lower() in IMAGE_EXT:
            groups.setdefault(group_of(p.stem), []).append(p)
    total = sum(len(v) for v in groups.values())
    if total == 0:
        print("Hata: görüntü bulunamadı.", file=sys.stderr)
        return 2
    if len(groups) < 2:
        print(f"Hata: yalnızca {len(groups)} maç/video grubu var. Maça göre bölmek için en az 2 farklı videodan kare gerekir.", file=sys.stderr)
        return 2

    assignment = split_groups(groups, args.val_ratio, args.seed)
    if out.exists():
        shutil.rmtree(out)
    counts = {"train": 0, "val": 0}
    missing_labels = 0
    for name, files in groups.items():
        part = assignment[name]
        (out / "images" / part).mkdir(parents=True, exist_ok=True)
        (out / "labels" / part).mkdir(parents=True, exist_ok=True)
        for img in files:
            shutil.copy2(img, out / "images" / part / img.name)
            label = labels_dir / f"{img.stem}.txt"
            if label.is_file():
                shutil.copy2(label, out / "labels" / part / label.name)
            else:
                missing_labels += 1  # etiketsiz kare: YOLO bunu "arka plan" sayar
            counts[part] += 1

    names_block = "\n".join(f"  {i}: {n}" for i, n in enumerate(args.classes))
    (out / "data.yaml").write_text(
        f"path: {out.resolve().as_posix()}\ntrain: images/train\nval: images/val\nnames:\n{names_block}\n",
        encoding="utf-8",
    )
    val_groups = sorted(n for n, p in assignment.items() if p == "val")
    print(f"{len(groups)} maç/video grubu, {total} görüntü -> eğitim {counts['train']} · doğrulama {counts['val']}")
    print("Doğrulamaya ayrılan maçlar:", ", ".join(val_groups))
    if missing_labels:
        print(f"Uyarı: {missing_labels} görüntünün etiket dosyası yok (arka plan sayılacak).")
    print(f"Hazır: {out / 'data.yaml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
