#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_devices.py —— 从 miuiver 站点地图自动生成机型表骨架。

    # 只生成国内稳定版（机型表最精简，日常扫描请求最少）
    python gen_devices.py -o devices.generated.json

    # 连各地区版本、开发版一起生成（几百上千条，慎用：每天扫描会很重）
    python gen_devices.py --all -o devices.full.json

生成的条目长这样：
    {"id": "xiaomi-15-cn", "brand": "xiaomi", "model": "dada", "region": "cn"}

id 用 codename + 区域，稳定不变；重跑不会打乱已有 id。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

SITEMAP = "https://miuiver.com/sitemap.xml"
UA = "Mozilla/5.0 (compatible; boot-farm/1.0)"

REGIONS = ("global", "eea", "in", "id", "ru", "tr", "tw", "jp", "kr", "sg")

# /tag/dada-stable-rom/  /tag/dada-tw-stable-rom/  /tag/dada-developer-rom/
TAG_RE = re.compile(
    r"https://miuiver\.com/tag/([a-z0-9_]+?)(?:-(global|eea|in|id|ru|tr|tw|jp|kr|sg))?"
    r"-(stable|developer)-rom/"
)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


ROMCLOUD = "https://roms.lian86.top"
MODEL_A_RE = re.compile(
    r'<a[^>]+href="/brand/([^/"]+)/model/([^/"?#]+)"[^>]*>(.*?)</a>', re.S
)
TAG_STRIP_RE = re.compile(r"<[^>]+>")


def run_romcloud(args) -> int:
    """从 RomCloud 的品牌页抓「机型名 ↔ 代号」，生成欧加机型表。

    这个站把国行全机型都列了（一加/OPPO/realme 各上百个），比手抄代号表省事。
    注意：它只是**目录**，版本页的下载链接有人机验证，不能自动取。
    """
    brands = [b.strip() for b in args.brand.split(",") if b.strip()]
    if not brands:
        print("romcloud 源需要 --brand（如 OnePlus,OPPO,Realme）", file=sys.stderr)
        return 1

    devices: dict[str, dict] = {}
    for b in brands:
        url = f"{ROMCLOUD}/brand/{b}"
        print(f"拉取 {url}", file=sys.stderr)
        try:
            html = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"  [fail] {b}: {e}", file=sys.stderr)
            continue

        n = 0
        for brand_path, code, inner in MODEL_A_RE.findall(html):
            if brand_path != b:
                continue
            name = TAG_STRIP_RE.sub("", inner).strip()
            # 卡片里名字后面还跟着一句提示，切掉
            name = re.split(r"进入后查看", name)[0].strip()
            if not name or code in {"model"}:
                continue
            rid = f"oplus-{code.lower()}"
            if rid in devices:
                continue
            devices[rid] = {
                "id": rid,
                "brand": "oplus",
                "model": code,
                "region": "cn",
                "display": name,
                "family": b.lower(),
                "source": f"{url}/model/{code}",
            }
            n += 1
        print(f"  {b}: {n} 个机型", file=sys.stderr)

    if not devices:
        print("没抓到机型", file=sys.stderr)
        return 1

    out = {
        "_comment": "由 gen_devices.py --source romcloud 生成；display 是机型中文名（仅供参考），"
                    "model 是 OTA 代号。下载链接仍需 tomboy_pro 现场解析。",
        "devices": [devices[k] for k in sorted(devices)],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"写入 {args.out}: {len(devices)} 个机型", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="devices.generated.json")
    ap.add_argument("--all", action="store_true", help="包含各地区版和开发版")
    ap.add_argument("--source", choices=["miuiver", "romcloud"], default="miuiver",
                    help="miuiver=小米；romcloud=欧加（一加/OPPO/realme）")
    ap.add_argument("--brand", default="", help="romcloud 源的品牌，逗号分隔")
    args = ap.parse_args()

    if args.source == "romcloud":
        return run_romcloud(args)

    print(f"拉取 {SITEMAP}", file=sys.stderr)
    xml = fetch(SITEMAP)

    # sitemap 可能是索引，再展开一层
    subs = re.findall(r"<loc>\s*(https://miuiver\.com/[^<]*sitemap[^<]*)\s*</loc>", xml)
    bodies = [xml]
    for s in subs[:5]:
        if s == SITEMAP:
            continue
        try:
            print(f"拉取子图 {s}", file=sys.stderr)
            bodies.append(fetch(s))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {s}: {e}", file=sys.stderr)

    seen: dict[str, dict] = {}
    for body in bodies:
        for m in TAG_RE.finditer(body):
            codename, region, kind = m.group(1), m.group(2), m.group(3)
            if not args.all and (region or kind != "stable"):
                continue
            rid = f"xiaomi-{codename}" + (f"-{region}" if region else "")
            seen[rid] = {
                "id": rid,
                "brand": "xiaomi",
                "model": codename,
                "region": region or "cn",
                "adapter_args": {"rom_type": kind},
            }

    if not seen:
        print("没解析出任何机型，站点地图结构可能变了", file=sys.stderr)
        return 1

    devices = [seen[k] for k in sorted(seen)]
    payload = {
        "_comment": "由 gen_devices.py 生成。加机型直接加条目；id 别改，去重表依赖它。",
        "_schema": {
            "id": "唯一标识，去重和产物命名都用它",
            "brand": "选 adapter：xiaomi / google / samsung / lolinet / manual / cmd",
            "model": "小米填机型代号(codename)，如 dada=小米15",
            "region": "cn/global/eea/in/id/ru/tr/tw/jp/kr/sg",
            "adapter_args": "透传给 adapter 的参数，如 rom_type=stable|developer",
        },
        "devices": devices,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"写入 {args.out}: {len(devices)} 个机型", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
