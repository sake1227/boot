#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
c16_resolve.py —— 解析 ColorOS / realme UI 的「动态下载链接」到最终 CDN 直链。

两种链接形态（依据 OPlus-Tracker 的 C16_transer.py）：

1) `https://component-ota-cn.allawntech.com/downloadCheck?c=..&g=..&id=..`
   只要两个请求头，服务端 302 到带签名的 CDN 直链：

       GET <url>
       userId: oplus-ota|00000001
       Range: bytes=0-
       （不跟随重定向，读 Location）

2) `https://component-ota-cn.allawntech.com/download?...`
   ColorOS 16 之后的新形态，要求在请求头里做 ECDSA 签名：
       ts / id / userId / Range / ac / as
   其中 `ac`、`as` 需要一份 Android **keybox.xml**（设备证明密钥）来签。
   本脚本不实现这一路：那不是「解析」，是拿设备证明密钥伪造客户端身份，
   脚本会明确提示并退出。

用法：
    python tools/c16_resolve.py "<downloadCheck url>"
    python tools/c16_resolve.py --file urls.txt          # 一行一个
    python tools/c16_resolve.py --file urls.txt -o out/resolved.tsv

输出：解析成功的直链 + 从 `Expires=` 推出的过期时间（这类链接通常 30 分钟~数小时）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import urllib.error
import urllib.parse
import urllib.request

# 和 C16_transer.py 一致的请求头（模拟 OTA 客户端，服务端据此放行）
OTA_HEADERS = {
    "userId": "oplus-ota|00000001",
    "Range": "bytes=0-",
    "User-Agent": "okhttp/4.9.0",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟随，我们要读 302 的 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def resolve(url: str, timeout: int = 20) -> tuple[str, int, str]:
    """返回 (最终直链, http状态, 说明)。"""
    if "downloadCheck?" in url:
        pass
    elif "/download?" in url or url.rstrip("/").endswith("/download"):
        return "", 0, ("这是需要 keybox 签名的新形态链接（download?），"
                       "本脚本不支持；见文件头说明")
    else:
        return "", 0, "不是 downloadCheck/download 链接"

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=OTA_HEADERS, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:
            return "", r.status, f"没有 302（HTTP {r.status}），链接可能已过期"
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if e.code in (301, 302, 303, 307, 308) and loc:
            return loc, e.code, "ok"
        return "", e.code, f"HTTP {e.code}（链接多半已过期或被拒）"
    except Exception as e:  # noqa: BLE001
        return "", -1, f"请求失败: {str(e)[:120]}"


def expires_of(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    for key in ("Expires", "x-oss-expires"):
        v = (q.get(key) or [None])[0]
        if v and v.isdigit():
            t = dt.datetime.fromtimestamp(int(v))
            return t.strftime("%Y-%m-%d %H:%M:%S")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="")
    ap.add_argument("--file", default="", help="一行一个 URL 的文件")
    ap.add_argument("-o", "--out", default="", help="结果写到 TSV")
    args = ap.parse_args()

    urls: list[str] = []
    if args.file:
        with open(args.file, encoding="utf-8-sig", errors="replace") as f:
            urls = [x.strip() for x in f if x.strip() and not x.startswith("#")]
    if args.url:
        urls.insert(0, args.url)
    if not urls:
        print("给一个 URL，或用 --file", file=sys.stderr)
        return 2

    rows, ok = [], 0
    for u in urls:
        final, code, note = resolve(u)
        if final:
            ok += 1
            exp = expires_of(final)
            print(f"OK   {code}  {final[:120]}")
            if exp:
                print(f"     过期时间: {exp}")
            rows.append((u, final, exp, note))
        else:
            print(f"FAIL {code}  {note}  <- {u[:100]}")
            rows.append((u, "", "", note))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write("\t".join(r) + "\n")
        print(f"\n写入 {args.out}")

    print(f"\n成功 {ok}/{len(urls)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
