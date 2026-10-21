#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
c16_resolve.py —— 解析 ColorOS / realme UI 的「动态下载链接」到最终 CDN 直链。

实测有效的机制（重要）：
    请求头 `userid: oplus-ota|`（注意末尾的竖线，**不要**带数字）
    → 服务器返回 HTTP 302 Found
    → Location 就是可下载的 CDN 直链（带 sign/Expires，约 30 分钟有效）

两种链接：
1) `.../downloadCheck?c=..&g=..&id=..` —— 用上面那个头即可，无需 keybox。
2) `.../download?...`（ColorOS 16 之后的新形态）—— 需要在头部做 ECDSA 签名
   （ts/id/ac/as），依赖 Android keybox.xml。本脚本不实现这一路：
   那是伪造设备证明身份，不是解析。

用法：
    python tools/c16_resolve.py "<downloadCheck url>"
    python tools/c16_resolve.py --file urls.txt -o out/resolved.tsv
    python tools/c16_resolve.py --quiet "<url>"      # 只打印解析出的直链

⚠️ 解析出来的直链寿命约 30 分钟，必须紧接着下载。
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 实测：必须是 oplus-ota| （末尾竖线）；带 00000001 会 429/报错
OTA_HEADERS = {
    "userid": "oplus-ota|",
    "Range": "bytes=0-",
    "User-Agent": UA,
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _resolve_with_curl(url: str, timeout: int = 30) -> tuple[str, str]:
    """用 curl 拿 302 的 Location（Actions / 主流 Linux 都有 curl）。"""
    cmd = ["curl", "-sI", "--max-time", str(timeout),
           "-H", "userid: oplus-ota|",
           "-H", "Range: bytes=0-",
           "-A", UA, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "", f"curl 失败: {e}"
    out = r.stdout or ""
    for line in out.splitlines():
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip(), ""
    return "", f"没有 302（{' / '.join(out.splitlines()[:2])[:120]}）"


def _resolve_with_urllib(url: str, timeout: int = 30) -> tuple[str, str]:
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers=OTA_HEADERS, method="HEAD")
    try:
        with opener.open(req, timeout=timeout) as r:
            return "", f"没有 302（HTTP {r.status}）"
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if loc:
            return loc, ""
        return "", f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return "", f"{type(e).__name__}: {str(e)[:100]}"


def resolve(url: str, timeout: int = 30) -> tuple[str, str]:
    """返回 (最终直链, 错误说明)。"""
    if url.rstrip("/").endswith("/download") or "/download?" in url:
        return "", "需要 keybox 签名的新形态链接（download?），本脚本不处理"

    fn = _resolve_with_curl if shutil.which("curl") else _resolve_with_urllib
    loc, err = fn(url, timeout)
    if not loc:
        return "", err or "未拿到 Location"

    try:
        r = urllib.request.urlopen(urllib.request.Request(
            loc, headers={"User-Agent": UA, "Range": "bytes=0-1023"}), timeout=timeout)
        if r.status not in (200, 206):
            return loc, f"直链 HTTP {r.status}"
        r.read(16)
    except Exception as e:  # noqa: BLE001
        return loc, f"直链校验失败 {type(e).__name__}"
    return loc, ""


def expires_of(url: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    v = (q.get("Expires") or [None])[0]
    if v and v.isdigit():
        return dt.datetime.fromtimestamp(int(v)).strftime("%Y-%m-%d %H:%M:%S")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default="")
    ap.add_argument("--file", default="", help="一行一个 URL 的文件")
    ap.add_argument("-o", "--out", default="", help="结果写到 TSV")
    ap.add_argument("--quiet", action="store_true", help="只输出解析出来的 URL")
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
        final, err = resolve(u)
        if final and not err:
            ok += 1
            exp = expires_of(final)
            if args.quiet:
                print(final)
            else:
                print(f"OK   {final[:140]}")
                if exp:
                    print(f"     过期: {exp}（约 30 分钟，立刻下载）")
            rows.append((u, final, exp, ""))
        else:
            if not args.quiet:
                print(f"FAIL {err}  <- {u[:100]}")
            rows.append((u, final, "", err))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            for r in rows:
                f.write("\t".join(r) + "\n")
        print(f"\n写入 {args.out}", file=sys.stderr)

    if not args.quiet:
        print(f"\n成功 {ok}/{len(urls)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
