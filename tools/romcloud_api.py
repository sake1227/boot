#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
romcloud_api.py —— 直接调 roms.lian86.top 的接口拿「机型全部版本 + 真实下载链接」。

接口是从它前端 chunk 里扒出来的（`/_nuxt/DobL7RqP.js`）：

    GET  /proxy/roms/versions?brand=OnePlus&model=PJZ110      ← 不要验证，直接可用
    GET  /proxy/roms/link?brand=..&model=..&version=..&...     ← 要 Authorization: Bearer <token>

token 来自站点的人机验证（cap.js 的 proof-of-work）。**本脚本不实现验证求解**，
需要你在浏览器里手动过一次验证，然后把浏览器发出的那个请求的
`Authorization: Bearer xxx` 复制过来。

用法
----
1) 列版本（不需要 token）：

    python tools/romcloud_api.py versions --brand OnePlus --model PJZ110

2) 取某个版本的下载链接（需要 token）：

    python tools/romcloud_api.py link --brand OnePlus --model PJZ110 \
        --version "ColorOS PJZ110_17.0.0.103(CN01)" --token "xxxxx"

3) 一次性把该机型所有版本的链接都取出来（token 没过期前尽量快）：

    python tools/romcloud_api.py dump --brand OnePlus --model PJZ110 \
        --token "xxxxx" -o out/links.tsv
    # 输出 TSV：brand<TAB>model<TAB>version<TAB>url<TAB>size<TAB>md5

怎么拿 token
------------
浏览器打开 https://roms.lian86.top/brand/OnePlus/model/PJZ110 ，点任意版本 →
完成人机验证 → F12 → Network → 找 `link` 这个请求（/proxy/roms/link）→
Request Headers → 复制 `Authorization` 的值（形如 `Bearer eyJ...`）。
把整串（含 Bearer）或只给 token 都能用，脚本会自动补前缀。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://roms.lian86.top"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "zh-CN,zh;q=0.9",
      "Referer": BASE + "/"}


def api(path: str, params: dict, token: str = "", timeout: int = 40):
    """调一个 /proxy 接口，返回 (status, payload)。"""
    url = BASE + path + "?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v not in (None, "")}
    )
    headers = dict(UA)
    if token:
        headers["Authorization"] = token if token.lower().startswith("bearer ") \
            else f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return e.code, {"error": body or e.reason}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": str(e)[:200]}


def cmd_versions(args) -> int:
    st, data = api("/proxy/roms/versions",
                   {"brand": args.brand, "model": args.model})
    if st != 200 or data.get("status") != "success":
        print(f"失败 {st}: {data}", file=sys.stderr)
        return 1
    items = data.get("data") or []
    print(f"{args.brand}/{args.model} 共 {len(items)} 个版本：")
    for it in items:
        extra = []
        for k in ("packageType", "channel", "modelVariantId"):
            if it.get(k):
                extra.append(f"{k}={it[k]}")
        types = it.get("availablePackageTypes") or []
        if types:
            extra.append("types=" + ",".join(map(str, types)))
        print(f"  {it.get('version')}   {' '.join(extra)}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        print(f"\n已写入 {args.out}")
    return 0


def fetch_links(args, ver: dict) -> list[dict]:
    params = {
        "brand": args.brand,
        "model": args.model,
        "version": ver.get("version"),
        "packageType": ver.get("packageType"),
        "channel": ver.get("channel"),
        "modelVariantId": ver.get("modelVariantId"),
    }
    st, data = api("/proxy/roms/link", params, token=args.token)
    if st != 200 or data.get("status") != "success":
        print(f"  [!] {ver.get('version')}: HTTP {st} {str(data)[:120]}", file=sys.stderr)
        return []
    d = data.get("data") or {}
    links = d.get("links") or []
    details = d.get("linkDetails") or []
    out = []
    for i, u in enumerate(links):
        det = details[i] if i < len(details) else {}
        out.append({
            "brand": args.brand,
            "model": args.model,
            "version": ver.get("version"),
            "url": u,
            "size": det.get("fileSize") or "",
            "md5": det.get("md5") or "",
            "filename": det.get("fileName") or "",
        })
    return out


def cmd_link(args) -> int:
    if not args.token:
        print("需要 --token（浏览器里手动验证后复制 Authorization）", file=sys.stderr)
        return 2
    got = fetch_links(args, {"version": args.version})
    if not got:
        print("没拿到链接", file=sys.stderr)
        return 1
    for x in got:
        print(f"{x['version']}\n  {x['url']}\n  size={x['size']} md5={x['md5']}")
    return 0


def cmd_dump(args) -> int:
    if not args.token:
        print("需要 --token（浏览器里手动验证后复制 Authorization）", file=sys.stderr)
        return 2
    st, data = api("/proxy/roms/versions",
                   {"brand": args.brand, "model": args.model})
    if st != 200 or data.get("status") != "success":
        print(f"取版本列表失败 {st}: {data}", file=sys.stderr)
        return 1
    items = data.get("data") or []
    print(f"{len(items)} 个版本，开始取链接（token 过期就重来）")
    rows, fails = [], 0
    for i, it in enumerate(items, 1):
        got = fetch_links(args, it)
        if got:
            rows.extend(got)
            print(f"  [{i}/{len(items)}] {it.get('version')} -> {len(got)} 条")
        else:
            fails += 1
        time.sleep(args.sleep)
    out = args.out or "out/romcloud_links.tsv"
    import pathlib
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for x in rows:
            f.write("\t".join([x["brand"], x["model"], x["version"],
                               x["url"], x["size"], x["md5"]]) + "\n")
    print(f"\n成功 {len(rows)} 条 / 失败 {fails} 个版本 -> {out}")

    if args.links_txt:
        # manual 适配器格式：名称|url，名称用版本号做产物名
        import re as _re
        with open(args.links_txt, "w", encoding="utf-8") as f:
            f.write("# 由 tools/romcloud_api.py dump 生成\n")
            for x in rows:
                name = _re.sub(r"[^\w.\-()]+", "_", x["version"]).strip("_")
                f.write(f"{name}|{x['url']}\n")
        print(f"已写出 {args.links_txt}（{len(rows)} 行，可直接给 manual 适配器）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("versions", "link", "dump"):
        p = sub.add_parser(name)
        p.add_argument("--brand", required=True, help="OnePlus / OPPO / Realme")
        p.add_argument("--model", required=True, help="机型代号，如 PJZ110")
        p.add_argument("--token", default="", help="Authorization 头（可含 Bearer）")
        p.add_argument("--out", default="")
        p.add_argument("--sleep", type=float, default=1.5, help="每个版本之间的间隔秒")
        p.add_argument("--links-txt", default="",
                       help="同时写出 links.txt（名称|url），给 manual 适配器用")
        if name == "link":
            p.add_argument("--version", required=True)

    args = ap.parse_args()
    return {"versions": cmd_versions, "link": cmd_link, "dump": cmd_dump}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
