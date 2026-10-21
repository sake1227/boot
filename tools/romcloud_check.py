#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
romcloud_check.py —— 用 RomCloud 的「机型页版本清单」对账，告诉你还缺哪些版本号。

它**只拿版本号，不拿下载链接**（版本页的链接在过人机验证后才由接口下发，
页面 HTML 里没有，见 README 的实测记录）。

用法：
    # 看某机型官方出过哪些版本
    python tools/romcloud_check.py --brand OnePlus --code PJZ110 --show

    # 和网盘已有清单对账（rclone lsf 的输出）
    rclone lsf 123:zanfang -R --files-only > remote.txt
    python tools/romcloud_check.py --brand OnePlus --code PJZ110 --listing remote.txt

    # 整个品牌的机型级对账（只列出缺的）
    python tools/romcloud_check.py --brand OPPO --all --listing remote.txt
"""
from __future__ import annotations

import argparse
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://roms.lian86.top"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"}
socket.setdefaulttimeout(30)


def get(url: str) -> str:
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)
        return r.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return ""


def models(brand: str) -> list[tuple[str, str]]:
    html = get(f"{BASE}/brand/{brand}")
    out = []
    for code, inner in re.findall(
        r'href="/brand/[^/"]+/model/([^/"?#]+)"[^>]*>(.*?)</a>', html, re.S
    ):
        name = re.sub(r"<[^>]+>", "", inner)
        name = re.split(r"进入后查看", name)[0].strip()
        out.append((code, name or code))
    seen, uniq = set(), []
    for c, n in out:
        if c in seen:
            continue
        seen.add(c)
        uniq.append((c, n))
    return uniq


def versions(brand: str, code: str) -> list[str]:
    html = get(f"{BASE}/brand/{brand}/model/{code}")
    raw = set()
    for v in re.findall(rf"/version/([^\"'?#]+)", html):
        raw.add(urllib.parse.unquote(v))
    # 只留「ColorOS/realme UI/OxygenOS + 版本号」这种
    out = []
    for v in raw:
        v = v.strip()
        if re.search(r"[0-9]+\.[0-9]+", v):
            out.append(v)
    return sorted(out, reverse=True)


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", s).lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True, help="OnePlus / OPPO / Realme")
    ap.add_argument("--code", default="", help="机型代号，如 PJZ110")
    ap.add_argument("--all", action="store_true", help="整个品牌所有机型")
    ap.add_argument("--listing", default="", help="rclone lsf 的输出，用来对账")
    ap.add_argument("--show", action="store_true", help="直接列出全部版本号")
    args = ap.parse_args()

    have = ""
    if args.listing:
        try:
            with open(args.listing, encoding="utf-8-sig", errors="replace") as f:
                have = norm(f.read())
        except OSError as e:
            print(f"读不了 {args.listing}: {e}", file=sys.stderr)
            return 1

    targets = models(args.brand) if args.all else [(args.code, args.code)]
    if not targets or (not args.all and not args.code):
        print("要么给 --code，要么给 --all", file=sys.stderr)
        return 1

    total_missing = 0
    for code, name in targets:
        vs = versions(args.brand, code)
        if not vs:
            continue
        missing = [v for v in vs if have and norm(v) not in have]
        if args.show:
            print(f"\n{name} ({code}) — 官方 {len(vs)} 个版本：")
            for v in vs:
                flag = ""
                if have:
                    flag = "  [已有]" if norm(v) in have else "  [缺]"
                print(f"  {v}{flag}")
        elif have:
            if missing:
                print(f"{name} ({code}): 缺 {len(missing)}/{len(vs)}")
                for v in missing:
                    print(f"    - {v}")
        else:
            print(f"{name} ({code}): {len(vs)} 个版本")
        total_missing += len(missing) if have else 0

    if have:
        print(f"\n合计缺 {total_missing} 个版本（版本号口径；包体需人工或降级接口获取）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
