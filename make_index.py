#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_index.py —— 扫 123 网盘目录，生成站点可以直接吃的数据文件。

    rclone lsf 123:zhanfang -R --files-only --format pst > listing.txt
    python make_index.py listing.txt -o data/index.json

`rclone lsf --format pst` 每行： 路径;大小;修改时间
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path


def human(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} TB"


def parse_listing(path: Path, strip_prefix: str):
    """一次读完，返回 (entries, metas)。"""
    entries: list[tuple[str, int, str]] = []
    metas: dict[str, dict] = {}

    for line in path.read_text("utf-8-sig", "replace").splitlines():
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(";")
        p = parts[0].strip().lstrip("\ufeff")
        size = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
        mtime = parts[2].strip() if len(parts) > 2 else ""
        if strip_prefix and p.startswith(strip_prefix):
            p = p[len(strip_prefix):]
        if not p:
            continue
        if p.lower().endswith(".json"):
            metas[p[:-5]] = {}
        else:
            entries.append((p, size, mtime))
    return entries, metas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("listing", help="rclone lsf --format pst 的输出文件")
    ap.add_argument("-o", "--out", default="index.json")
    ap.add_argument("--strip-prefix", default="", help="去掉路径前缀，如 zhanfang/")
    ap.add_argument("--ext", default=".zip", help="只收录该扩展名（默认 .zip）")
    ap.add_argument("--meta-dir", default="",
                    help="extract_one.sh 写的 *.json 元数据目录（可选）")
    args = ap.parse_args()

    meta_root = Path(args.meta_dir) if args.meta_dir else None

    src = Path(args.listing)
    if not src.exists():
        print(f"找不到 {src}", file=sys.stderr)
        return 1

    entries, metas = parse_listing(src, args.strip_prefix)
    if not entries:
        print(f"{src} 里没有可识别的文件", file=sys.stderr)

    devices: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    total = 0

    for p, size, mtime in entries:
        base = p.rsplit("/", 1)[-1]
        if not base.lower().endswith(args.ext):
            continue

        bits = p.split("/")
        if len(bits) >= 3:
            brand, model = bits[0], bits[1]
        elif len(bits) == 2:
            brand, model = bits[0], "_"
        else:
            brand, model = "_", "_"

        stem = p[: -len(args.ext)]
        meta = metas.get(stem, {})
        if meta_root is not None and not meta:
            mf = meta_root / f"{stem}.json"
            if mf.exists():
                try:
                    meta = json.loads(mf.read_text("utf-8"))
                except json.JSONDecodeError:
                    meta = {}
        devices[brand][model].append(
            {
                "name": base[: -len(args.ext)],
                "zip": p,
                "bytes": size,
                "size": human(size),
                "mtime": mtime,
                "images": meta.get("images", []),
                "version": meta.get("version", ""),
                "source_url": meta.get("source_url", ""),
            }
        )
        total += 1

    for brand in devices:
        for model in devices[brand]:
            devices[brand][model].sort(
                key=lambda x: (x["mtime"], x["name"]), reverse=True
            )

    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_files": total,
        "devices": {b: dict(m) for b, m in devices.items()},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), "utf-8"
    )
    n_model = sum(len(m) for m in devices.values())
    print(f"写入 {args.out}: {total} 个包 / {len(devices)} 品牌 / "
          f"{n_model} 机型", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
