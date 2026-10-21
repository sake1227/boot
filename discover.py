#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多源 ROM 直链发现器 —— 零第三方依赖，可直接在 GitHub Actions 里跑。

    # 列出某机型所有新版本（不下载）
    python discover.py --devices devices.json --only xiaomi-15 --print

    # 生成 Actions matrix
    python discover.py --devices devices.json --matrix-out out/matrix.json

设计要点
--------
1. 解析按 **URL 语法** 而不是 HTML 标签，对方改页面模板不会直接挂。
   版本号从 URL slug 取（/dada-v2-0-216/ -> 2.0.216），下载链从扩展名取。
2. candidate.stable = True  永久直链，可以离线慢慢囤；
   candidate.stable = False 动态签名链接，10~30 分钟过期，必须解析完立刻下载。
3. 去重键优先用 adapter 给的 key（如 id|v2.0.216|xxx.zip），
   这样已知版本连详情页都不用抓 —— 每天扫描的请求量能压到个位数。
4. 每个 device 独立 try/except，一家接口坏了不影响其他品牌。

加新品牌 = 在 ADAPTERS 里加一个函数。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 只认这些扩展名，避免把图片/网页当包
ROM_EXT_RE = re.compile(r"\.(zip|ozip|tgz|tar\.md5|7z|bin)$", re.I)
ANY_URL_RE = re.compile(r"""https?://[^\s"'<>()\\\]]+""")
FILE_RE = ROM_EXT_RE

# GitHub matrix 上限 256，留点余量
MAX_MATRIX = 240


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: int = 40, retries: int = 3) -> str:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return raw.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed: {url} ({last})")


def fetch_cached(url: str, ctx: dict) -> str:
    """详情页缓存 7 天，重跑不会重复打对方站点。"""
    d = Path(ctx["cache_dir"])
    key = hashlib.sha1(url.encode()).hexdigest()
    p = d / f"{key}.html"
    if p.exists() and time.time() - p.stat().st_mtime < 7 * 86400:
        return p.read_text("utf-8", "replace")
    txt = fetch(url)
    try:
        d.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, "utf-8")
    except OSError:
        pass
    return txt


def candidate(dev: dict, **kw) -> dict:
    c = {
        "device_id": dev["id"],
        "brand": dev.get("brand", "?"),
        "model": dev.get("model", "?"),
        "region": dev.get("region", ""),
        "version": "",
        "filename": "",
        "devname": "",     # 机型中文名，用作网盘目录
        "cat": "",         # 分类，如「数字系列」「Note 系列」
        "brandcn": "",     # 品牌中文名，网盘第一层：小米/红米/一加/真我/OPPO/魅族…
        "referer": "",     # 下载时需要的 Referer（魅族 CDN 强制）
        "targets": [],     # 显式指定的上传目标目录（通用机型）
        "optional": False, # 解析不到就静默跳过（欧加分支探测）
        "url": "",
        "size": 0,
        "md5": "",
        "stable": False,
        "status": "ready",
        "requires": "",
        "source": "",
        "resolve": "",
        "key": "",
    }
    c.update(kw)
    if not c["filename"]:
        c["filename"] = os.path.basename(urllib.parse.urlparse(c["url"]).path)
    return c


def dispatchable(c: dict) -> bool:
    """能进 matrix 的：有现成直链，或者给了 setup 阶段解析不了的 resolve 命令。"""
    if c["status"] == "ready" and c["url"]:
        return True
    return c["status"] == "resolve" and bool(c["resolve"])


def dedupe_key(c: dict) -> str:
    return c["key"] or f"{c['device_id']}|{c['filename']}"


def clean_url(u: str) -> str:
    return u.rstrip(".,;)\\]\"'")


def stem_of(filename: str) -> str:
    """去掉 ROM 扩展名：x.tar.md5 -> x，y.tgz -> y"""
    s = re.sub(r"\.(tar\.md5|zip|ozip|tgz|tar|7z|bin)$", "", filename, flags=re.I)
    return s or filename


# --------------------------------------------------------------------------- #
# Adapter: 小米 / 红米 / POCO —— 索引页 + 官方 CDN 直链
#
# 索引页: https://miuiver.com/tag/{codename}[-{region}]-{stable|developer}-rom/
#   列出该机型**全部历史版本**，新版在前，每版一个详情页
# 详情页: https://miuiver.com/{codename}-v2-0-216/
#   直接挂官方直链，形如
#   https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/
#       OS2.0.216.0.VOCCNXM/dada-ota_full-OS2.0.216.0.VOCCNXM-user-15.0-6703365c83.zip
#
# 只抓索引页 + 新版本的详情页，已知版本一次请求都不发。
# --------------------------------------------------------------------------- #
XIAOMI_REGION = {
    "cn": "",
    "global": "-global",
    "eea": "-eea",
    "eu": "-eea",
    "in": "-in",
    "id": "-id",
    "ru": "-ru",
    "tr": "-tr",
    "tw": "-tw",
    "jp": "-jp",
    "kr": "-kr",
    "sg": "-sg",
}

# 详情页里出现的官方 ROM 域名特征（防止把站内的 zip/图片误当固件）
XIAOMI_CDN_HINTS = (
    "aliyuncs.com",
    "miui.com",
    "xiaomi.com",
    "mi-img.com",
    "cdnorg",
    "bigota",
    "hugeota",
)


def xiaomi_kind(name: str) -> str:
    """小米一个版本会挂好几个包，必须分清，否则增量包会白跑一个 job。

    ota_full       卡刷包，内含完整 payload.bin   <- 提 boot 首选
    images         线刷包 tgz，内含 images/boot.img
    incremental    增量包，差分 payload，解不出完整分区，必须跳过
    """
    low = name.lower()
    if "incremental" in low or "delta" in low:
        return "incremental"
    if low.endswith(".tgz") or "images_" in low:
        return "images"
    if "ota_full" in low:
        return "ota_full"
    return "other"


# 机型目录（代号 -> 中文名 + 系列），roms.miuier.com 一个请求拿全部机型
MIUIER_PAYLOAD = "https://roms.miuier.com/zh-cn/devices/_payload.json"

# 分类兜底规则：目录里查不到时（欧加、Pixel、摩托…）按名字猜
CATEGORY_RULES = (
    ("平板", "平板系列"), ("Pad", "平板系列"),
    ("Note", "Note 系列"),
    ("Ace", "Ace 系列"),
    ("Turbo", "Turbo 系列"),
    ("Reno", "Reno 系列"),
    ("Find", "Find 系列"),
    ("GT", "GT 系列"),
    ("POCO", "POCO 系列"),
    ("MIX", "MIX 系列"),
    ("K", "K 系列"),
)


def normalize_series(series: str) -> str:
    """把「小米数字系列」「Redmi Note 系列」统一成「数字系列」「Note 系列」。
    这是修复「小米13 Ultra 落在小米数字系列、小米15 落在数字系列」两个目录的根因。"""
    s = (series or "").strip()
    if not s:
        return ""
    for p in ("小米", "红米", "Redmi", "POCO", "一加", "OPPO", "真我", "Realme", "魅族"):
        if s.startswith(p):
            s = s[len(p):].strip()
            break
    s = s.replace("设备", "").strip()
    if s.startswith("其他"):
        return "其他"
    if s == "平板":
        return "平板系列"
    return s


def category_of(name: str, series: str = "") -> str:
    series = normalize_series(series)
    if series:
        return series
    for key, cat in CATEGORY_RULES:
        if key in (name or ""):
            return cat
    return "数字系列"


# 品牌中文名：网盘第一层目录
BRAND_CN = {
    "google": "Google",
    "samsung": "三星",
    "lolinet": "摩托罗拉",
    "meizu": "魅族",
    "xiaomi": "小米",
    "oplus": "OPPO",
}


def brand_cn(brand: str, devname: str = "") -> str:
    """按机型名优先判品牌（小米/红米/POCO 同属 xiaomi adapter），判不出来再用 brand 兜底。"""
    n = devname or ""
    if n.startswith("一加"):
        return "一加"
    if n.startswith("OPPO"):
        return "OPPO"
    if n.lower().startswith("realme") or n.startswith("真我"):
        return "真我"
    if n.startswith("魅族") or n.startswith("魅蓝"):
        return "魅族"
    if n.startswith("红米") or n.startswith("Redmi"):
        return "红米"
    if n.startswith("POCO"):
        return "POCO"
    if n.startswith("小米"):
        return "小米"
    if n.startswith("联想"):
        return "联想"
    if n.upper().startswith("IQOO"):
        return "iQOO"
    if n.lower().startswith("vivo"):
        return "vivo"
    return BRAND_CN.get(brand, brand or "其他")


def _hydrate_payload(data: list):
    """解码 Nuxt 的 devalue 序列化：整数是索引引用，解一次后若是数字即字面量。"""

    def h(node, depth=0):
        if depth > 100:
            return None
        if isinstance(node, bool):
            return node
        if isinstance(node, int):
            if node < 0 or node >= len(data):
                return node
            tgt = data[node]
            if isinstance(tgt, bool) or isinstance(tgt, int):
                return tgt
            return h(tgt, depth + 1)
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return [h(x, depth + 1) for x in node]
        if isinstance(node, dict):
            return {k: h(v, depth + 1) for k, v in node.items()}
        return node

    return h


def _find_key(node, key: str, depth: int = 0):
    if depth > 40:
        return None
    if isinstance(node, dict):
        if key in node:
            return node
        for v in node.values():
            got = _find_key(v, key, depth + 1)
            if got:
                return got
    elif isinstance(node, list):
        for v in node[:2000]:
            got = _find_key(v, key, depth + 1)
            if got:
                return got
    return None


def load_device_catalog(ctx: dict) -> dict:
    """代号 -> {name, series}。缓存 7 天；拿不到就返回空表，不影响主流程。"""
    cache = Path(ctx["cache_dir"]) / "miuier-devices.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        try:
            return json.loads(cache.read_text("utf-8"))
        except json.JSONDecodeError:
            pass

    out: dict = {}
    try:
        payload = json.loads(fetch(MIUIER_PAYLOAD))
        root = _hydrate_payload(payload)(payload[0])
        holder = _find_key(root, "brands") or {}
        for brand in holder.get("brands") or []:
            bname = (brand.get("zh-cn") or "").replace("设备", "").replace(" 手机", "").strip()
            for series in brand.get("series") or []:
                sname = series.get("zh-cn") or ""
                for dev in series.get("devices") or []:
                    code = dev.get("code")
                    if not code:
                        continue
                    out[code] = {"name": dev.get("zh-cn") or code,
                                 "series": sname, "brand": bname}
        print(f"  [catalog] {len(out)} 个机型", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [catalog] 拉取失败，改用兜底规则: {e}", file=sys.stderr)

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return out


def ver_from_filename(name: str) -> str:
    """从原始文件名抠版本号：dada-ota_full-OS3.0.307.0.WOCCNXM-...zip -> OS3.0.307.0.WOCCNXM"""
    m = re.search(r"((?:OS|V)\d[\d.]*[A-Z]{2,})", name or "")
    return m.group(1) if m else ""


def ad_xiaomi(dev: dict, ctx: dict) -> list[dict]:
    args = dev.get("adapter_args") or {}
    codename = dev["model"]
    # 默认只要卡刷包：一版一个包，体积和时间都省一半
    wanted = set(args.get("kinds") or ["ota_full"])

    tag = args.get("tag")
    if not tag:
        suffix = XIAOMI_REGION.get((dev.get("region") or "cn").lower(), "")
        kind = args.get("rom_type", "stable")
        tag = f"{codename}{suffix}-{kind}-rom"

    tag_url = f"https://miuiver.com/tag/{tag}/"
    html = fetch(tag_url)

    # 机型中文名 / 系列：优先用 miuier 机型目录，其次从索引页 H1 抠，最后用代号
    catalog = ctx.setdefault("catalog", load_device_catalog(ctx))
    info = catalog.get(codename) or {}
    devname = (dev.get("display") or "").strip() or (info.get("name") or "").strip()
    if not devname:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        if m:
            devname = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            devname = re.split(r"(?:正式版|开发版|刷机包|下载合集|合集)", devname)[0].strip()
    devname = devname or codename
    cat = category_of(devname, dev.get("series") or info.get("series") or "")
    bcn = dev.get("brandcn") or info.get("brand") or brand_cn("xiaomi", devname)

    # 用 URL 语法取版本：/dada-v2-0-216/ -> (2, 0, 216)
    found: dict[str, tuple[int, ...]] = {}
    for m in re.finditer(r"https://miuiver\.com/[a-z0-9_.]+-v(\d+(?:-\d+)*)/", html):
        ver = tuple(int(x) for x in m.group(1).split("-"))
        found[m.group(0)] = ver
    if not found:
        print(f"  [warn] {dev['id']}: 索引页没有版本条目 ({tag_url})", file=sys.stderr)
        return []

    ordered = sorted(found.items(), key=lambda kv: kv[1], reverse=True)
    maxn = int(args.get("max_new_per_device", ctx["max_new_per_device"]))

    out: list[dict] = []
    for detail_url, ver in ordered:
        if len(out) >= maxn:
            break
        vstr = ".".join(map(str, ver))
        key = f"{dev['id']}|v{vstr}"
        if any(k.startswith(key + "|") for k in ctx["seen"]):
            continue

        try:
            detail = fetch_cached(detail_url, ctx)
        except RuntimeError as e:
            print(f"  [warn] {e}", file=sys.stderr)
            continue

        got = 0
        for raw in ANY_URL_RE.findall(detail):
            url = clean_url(raw)
            if not ROM_EXT_RE.search(urllib.parse.urlparse(url).path):
                continue
            low = url.lower()
            if not any(h in low for h in XIAOMI_CDN_HINTS):
                continue  # 站内跳转/统计链接一律不要
            name = os.path.basename(urllib.parse.urlparse(url).path)
            kind = xiaomi_kind(name)
            if kind not in wanted:
                continue
            # 产物名用版本号：OS3.0.307.0.WOCCNXM.zip（比原始长文件名干净，且去重稳定）
            vname = ver_from_filename(name) or vstr
            out.append(
                candidate(
                    dev,
                    filename=f"{vname}.zip",
                    url=url,
                    version=vname,
                    devname=devname,
                    cat=cat,
                    brandcn=bcn,
                    stable=True,  # 官方 CDN 永久直链
                    source=detail_url,
                    key=f"{key}|{vname}",
                )
            )
            got += 1
        if got == 0:
            print(f"  [warn] {dev['id']} v{vstr}: 没找到符合条件的包"
                  f"（kinds={sorted(wanted)}，可能要登录），跳过", file=sys.stderr)
        time.sleep(ctx["sleep"])

    return out


# --------------------------------------------------------------------------- #
# Adapter: Google Pixel —— 永久直链
# --------------------------------------------------------------------------- #
GOOGLE_IMAGES = "https://developers.google.com/android/images"


def ad_google(dev: dict, ctx: dict) -> list[dict]:
    args = dev.get("adapter_args") or {}
    kind = args.get("kind", "factory")
    html = fetch(GOOGLE_IMAGES)
    if kind == "ota":
        html += fetch("https://developers.google.com/android/ota")

    codename = dev["model"]
    # 真实文件名: shiba-ap2a.240905.003-factory-2e0e57a6.zip（build 段可能缺失）
    pat = re.compile(
        r"(https://dl\.google\.com/dl/android/aosp/"
        r"([a-z0-9_]+)-(?:([0-9a-z._]+)-)?(?:factory|ota)-([0-9a-f]+)\.zip)"
    )
    out, seen_names = [], set()
    for full, code, build, sha in pat.findall(html):
        if code != codename:
            continue
        name = os.path.basename(urllib.parse.urlparse(full).path)
        if name in seen_names:
            continue
        seen_names.add(name)
        out.append(candidate(dev, filename=name, url=full, version=build or sha,
                             stable=True, source=GOOGLE_IMAGES))
    out.sort(key=lambda c: c["filename"], reverse=True)
    return out[: int(args.get("max_new_per_device", ctx["max_new_per_device"]))]


# --------------------------------------------------------------------------- #
# Adapter: 三星 —— 版本号免费拿；包体需 samloader 认证
# --------------------------------------------------------------------------- #
def ad_samsung(dev: dict, ctx: dict) -> list[dict]:
    csc = dev.get("region") or ""
    model = dev["model"]
    if not csc:
        raise ValueError("samsung adapter 需要 region(CSC)")

    url = f"https://fota-cloud-dn.ospserver.net/firmware/{csc}/{model}/version.xml"
    xml = fetch(url)
    try:
        root = ET.fromstring(xml.strip().lstrip("\ufeff"))
    except ET.ParseError:
        return []  # 机型/CSC 组合不存在时服务器给空体

    best = None
    for el in root.iter():
        if el.tag.lower() != "latest":
            continue
        ver = (el.text or "").strip()
        if not ver:
            continue
        item = {"version": ver, "android": int(el.get("o") or 0),
                "fwsize": int(el.get("fwsize") or 0)}
        if best is None or item["android"] > best["android"]:
            best = item
    if not best:
        return []

    return [
        candidate(
            dev,
            filename=f"{model}_{csc}_{best['version']}.tar.md5",
            version=best["version"],
            size=best["fwsize"],
            stable=False,
            status="resolve",
            requires="samloader-fus",
            source=url,
            key=f"{dev['id']}|{best['version']}",
        )
    ]


# --------------------------------------------------------------------------- #
# Adapter: lolinet 镜像 BFS（h5ai 目录索引，永久直链）
# --------------------------------------------------------------------------- #
def ad_lolinet(dev: dict, ctx: dict) -> list[dict]:
    args = dev.get("adapter_args") or {}
    root = args.get("root") or "https://mirrors.lolinet.com/firmware/moto/"
    max_depth = int(args.get("max_depth", 3))
    needle = (args.get("match") or dev.get("model") or "").lower()

    out: list[dict] = []
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(root, 0)]

    while queue:
        url, depth = queue.pop(0)
        if url in visited or len(visited) > 400:
            continue
        visited.add(url)
        try:
            html = fetch(url)
        except RuntimeError as e:
            print(f"  [warn] {e}", file=sys.stderr)
            continue

        for href in set(re.findall(r'href="([^"#?]+)"', html)):
            full = clean_url(urllib.parse.urljoin(url, href))
            if not full.startswith("http"):
                continue
            low = full.lower()
            if FILE_RE.search(low):
                if needle and needle not in low:
                    continue
                name = os.path.basename(urllib.parse.urlparse(full).path)
                out.append(candidate(dev, filename=name, url=full,
                                     stable=True, source=url))
            elif low.endswith("/") and depth < max_depth:
                if needle and needle not in low and depth == max_depth - 1:
                    continue
                queue.append((full, depth + 1))
        time.sleep(ctx["sleep"])
    return out


# --------------------------------------------------------------------------- #
# Adapter: manual 手工链接文件
# --------------------------------------------------------------------------- #
def ad_manual(dev: dict, ctx: dict) -> list[dict]:
    path = Path(dev.get("model") or "links.txt")
    if not path.is_absolute():
        for base in (Path(ctx["base_dir"]), Path.cwd()):
            if (base / path).exists():
                path = base / path
                break
        else:
            return []

    out: list[dict] = []
    for line in path.read_text("utf-8-sig", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, url = line.partition("|")
        if not url:
            url, name = name, ""
        url = clean_url(url.strip())
        if not ANY_URL_RE.match(url):
            continue
        fname = name.strip() or os.path.basename(urllib.parse.urlparse(url).path)
        out.append(candidate(dev, filename=fname, url=url, stable=False,
                             source="manual"))
    return out


# --------------------------------------------------------------------------- #
# Adapter: cmd —— 万能逃生口
# 用 shell 包一层社区实现（OPlus-Tracker / Updater / samloader），
# 从 stdout 抽 URL。厂商改加密只改外层脚本，不动本文件。
# --------------------------------------------------------------------------- #
def ad_cmd(dev: dict, ctx: dict) -> list[dict]:
    cmd = (dev.get("adapter_args") or {}).get("cmd")
    if not cmd:
        raise ValueError("cmd adapter 需要 adapter_args.cmd")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                         timeout=1800)
    text = (res.stdout or "") + (res.stderr or "")
    out = []
    for raw in dict.fromkeys(ANY_URL_RE.findall(text)):
        url = clean_url(raw)
        if not ROM_EXT_RE.search(urllib.parse.urlparse(url).path):
            continue
        out.append(candidate(dev, url=url, stable=False, source=f"cmd:{cmd}"))
    return out


# --------------------------------------------------------------------------- #
# Adapter: 欧加（OPPO / 一加 / realme）—— 动态链接，必须在下载前现场解析
#
# 这类机型没有永久直链：查询接口只给一个 downloadCheck? 动态 URL，10~30 分钟过期。
# 所以这里不解析链接，只发一条 resolve 命令进 matrix，由 extract_one.sh 在
# 下载的同一个 job 里「解析完立刻下」。
#
# 解析靠 OPlus-Tracker（公开仓库），workflow 里 git clone 到 tools/oplus：
#   python3 tools/oplus/tomboy_pro.py PJX110_11.A cn --anti 1 --mode taste --original_link 1
#
# adapter_args:
#   versions: ["PJX110_11.C.36_1360_20250814", ...]  指定版本逐个查（"各版本"用这个）
#   extra_flags: 额外的查询开关，默认 "--anti 1 --mode taste"
#   tool: 覆盖脚本路径
# --------------------------------------------------------------------------- #
OPLUS_TOOL = "tools/oplus/tomboy_pro.py"


def ad_oplus(dev: dict, ctx: dict) -> list[dict]:
    args = dev.get("adapter_args") or {}
    prefix = dev["model"]
    region = (dev.get("region") or "cn").lower()
    extra = args.get("extra_flags", "--anti 1 --mode taste")
    tool = args.get("tool", OPLUS_TOOL)
    versions = args.get("versions") or []

    # 没显式给 versions 时，逐个分支查（A/C/F/H/J），每分支取该分支最新完整包，
    # 这样一次运行能拿到同一机型最多 5 个版本，而不是只有 1 个
    BRANCHES = ("A", "C", "F", "H", "J")
    if versions:
        targets = list(versions)
    else:
        targets = [f"{prefix}_11.{b}" for b in BRANCHES]

    out: list[dict] = []
    display = (dev.get("display") or "").strip() or dev.get("label") or prefix
    cat = category_of(display, dev.get("category") or "")
    bcn = brand_cn("oplus", display)
    for target in targets:
        cmd = f"python3 {tool} {target} {region} {extra} --original_link 1"
        out.append(
            candidate(
                dev,
                # 产物名 = 版本号本身，例如 PJZ110_17.0.0.103(CN01).zip
                filename=f"{str(target)}.zip",
                version=str(target),
                model=dev.get("label") or dev["model"],
                devname=display,
                cat=cat,
                brandcn=bcn,
                stable=False,
                status="resolve",
                requires="OPlus-Tracker",
                optional=True,
                resolve=cmd,
                source=f"oplus:{target}",
                key=f"{dev['id']}|{target}",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# Adapter: 魅族（www.flyme.com/firmware.html）
#
# 三级结构：
#   1. firmware.html 里 var firmware=[...]，detail_data 是「机型 + phone_id」
#   2. firmwarelist-<phone_id>.html 里 var data={...}，每个变体给 版本/md5/大小/发布时间
#      和 download 字段（/zh/download?key=xxx）
#   3. 那个 key 页会 302 到 firmware-res.flyme.com/..../update.zip?auth_key=...
#      auth_key 会过期，所以同样的「下载 job 里现场解析」
# --------------------------------------------------------------------------- #
FLYME_INDEX = "https://www.flyme.com/firmware.html"
FLYME_LIST = "https://www.flyme.com/firmwarelist-{pid}.html"


def _js_bracket(text: str, start: int) -> str:
    """从 start 处的 [ 或 { 开始做括号配对，返回完整字面量文本。"""
    depth = 0
    for j in range(start, len(text)):
        ch = text[j]
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    return ""


def _loose_json(raw: str):
    """JS 对象字面量 -> JSON：单引号转双引号、裸 key 加引号、去尾逗号。"""
    s = raw.replace("'", '"')
    s = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', s)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return json.loads(s)


def load_flyme_phones(ctx: dict) -> dict:
    """phone_id -> 机型中文名。缓存 7 天。"""
    cache = Path(ctx["cache_dir"]) / "flyme-phones.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
        try:
            return json.loads(cache.read_text("utf-8"))
        except json.JSONDecodeError:
            pass

    out: dict = {}
    try:
        html = fetch(FLYME_INDEX)
        m = re.search(r"var\s+firmware\s*=\s*", html)
        data = json.loads(_js_bracket(html, html.index("[", m.end())))
        for brand in data:
            phones = brand.get("detail_data")
            phones = _loose_json(phones) if isinstance(phones, str) else phones
            for p in phones or []:
                pid = p.get("phone_id")
                if pid:
                    out[str(pid)] = p.get("phone_name") or f"魅族{pid}"
        print(f"  [flyme] {len(out)} 个机型", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [flyme] 机型列表拉取失败: {e}", file=sys.stderr)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out, ensure_ascii=False), "utf-8")
    except OSError:
        pass
    return out


def ad_meizu(dev: dict, ctx: dict) -> list[dict]:
    args = dev.get("adapter_args") or {}
    pid = str(dev["model"])
    phones = ctx.setdefault("flyme", load_flyme_phones(ctx))
    devname = (dev.get("display") or "").strip() or phones.get(pid) or f"魅族{pid}"

    cat = "数字系列"
    for k, c in (("Note", "Note 系列"), ("魅蓝", "魅蓝系列"), ("Lucky", "Lucky 系列")):
        if k in devname:
            cat = c
            break

    html = fetch(FLYME_LIST.format(pid=pid))
    m = re.search(r"var\s+data\s*=\s*", html)
    if not m:
        print(f"  [warn] {dev['id']}: 机型页没有 var data", file=sys.stderr)
        return []
    variants = list(json.loads(_js_bracket(html, html.index("{", m.end()))).values())
    multi = len(variants) > 1
    maxn = int(args.get("max_new_per_device", ctx["max_new_per_device"]))

    out: list[dict] = []
    for v in variants:
        if len(out) >= maxn:
            break
        name = (v.get("name") or "").strip()
        dl = (v.get("download") or "").strip()
        if not name or not dl:
            continue
        vstr = re.sub(r"[^\w.\-]+", "_", name.replace(" ", "_")).strip("_")
        if multi:
            net = re.sub(r"[^0-9A-Za-z]+", "", v.get("net_name") or "")
            if net and not vstr.upper().endswith(net.upper()):
                vstr = f"{vstr}_{net}"

        key = f"{dev['id']}|{vstr}"
        if any(k.startswith(key + "|") for k in ctx["seen"]):
            continue

        page = dl if dl.startswith("http") else "https://www.flyme.com" + dl
        # 302 到带 auth_key 的直链，必须下载前现场解析
        cmd = f"curl -sIL -o /dev/null -w '%{{url_effective}}\\n' '{page}'"
        out.append(
            candidate(
                dev,
                filename=f"{vstr}.zip",
                version=name,
                devname=devname,
                cat=cat,
                brandcn="魅族",
                referer="https://www.flyme.com/",
                stable=False,
                status="resolve",
                requires="flyme auth_key",
                resolve=cmd,
                source=page,
                key=f"{key}|{vstr}",
            )
        )
    return out


ADAPTERS = {
    "xiaomi": ad_xiaomi,
    "google": ad_google,
    "samsung": ad_samsung,
    "lolinet": ad_lolinet,
    "oplus": ad_oplus,
    "meizu": ad_meizu,
    "manual": ad_manual,
    "cmd": ad_cmd,
    # vivo/iQOO/联想：官方没有可直接抓的固件入口，用 cmd 适配器接第三方
}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def load_seen(path: Path) -> set[str]:
    if path.exists():
        try:
            return set(json.loads(path.read_text("utf-8-sig")).get("keys", []))
        except json.JSONDecodeError:
            pass
    return set()


BAD_DIR_CHARS = str.maketrans({"/": "、", "\\": "、", ":": "：", "*": "＊",
                              "?": "？", '"': "＂", "<": "＜", ">": "＞",
                              "|": "｜", ";": "，"})

def sanitize_dirname(name: str) -> str:
    """机型名里的 / \\ : * ? " < > | ; 都不能进目录名（否则会被当路径分隔符）。"""
    return (name or "").strip().translate(BAD_DIR_CHARS).strip()


def split_names(display: str) -> list[str]:
    """通用机型：「OPPO A58 / OPPO A58x / OPPO A1x」-> 三个机型名。"""
    parts = re.split(r"\s*/\s*", display or "")
    out, seen = [], set()
    for p in parts:
        p = p.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def build_targets(c: dict) -> list[dict]:
    """候选要落到哪些「品牌/分类/机型」目录。通用机型会拆成多个目录。"""
    if c.get("targets"):
        return c["targets"]
    devname = c["devname"] or c["model"]
    names = split_names(devname)
    if len(names) > 1:
        return [
            {"brandcn": brand_cn(c["brand"], n),
             "cat": category_of(n),
             "devname": sanitize_dirname(n)}
            for n in names
        ]
    return [{
        "brandcn": c["brandcn"] or brand_cn(c["brand"], devname),
        "cat": c["cat"] or category_of(devname),
        "devname": sanitize_dirname(devname),
    }]


def targets_str(c: dict) -> str:
    return ";".join(f"{t['brandcn']}|{t['cat']}|{t['devname']}" for t in build_targets(c))


def dest_rel(c: dict) -> str:
    """候选在网盘里的相对路径（取第一个目标），必须和 extract_one.sh 落地路径一致。"""
    t = build_targets(c)[0]
    b, cat, dev = t["brandcn"], t["cat"], t["devname"]
    name = re.sub(r"[^\w.\-()]+", "_", stem_of(c["filename"]))
    return f"{b}/{cat}/{dev}/{name}.zip"


def load_existing(path: Path) -> set:
    out = set()
    if not path.exists():
        return out
    for line in path.read_text("utf-8-sig", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        p = line.split(";")[0].strip().lstrip("/")
        if p:
            out.add(p)
    return out


def build_matrix(all_c: list[dict], max_total: int = MAX_MATRIX) -> dict:
    """只收能真正派发的：有直链，或有 resolve 命令（在下载 job 里现场解析）。"""
    picked = [c for c in all_c if dispatchable(c)]
    cap = min(max_total, MAX_MATRIX)
    if len(picked) > cap:
        print(f"[warn] matrix 超上限，截断到 {cap}（其余下次运行）", file=sys.stderr)
        picked = picked[:cap]
    return {
        "include": [
            {
                "id": c["device_id"],
                "brand": c["brand"],
                "model": c["model"],
                "version": c["version"],
                # 去掉原始扩展名，workflow 里统一补成 .zip，避免 a.zip.zip
                "name": re.sub(r"[^\w.\-()]+", "_", stem_of(c["filename"])),
                "cat": c["cat"] or category_of(c["devname"] or c["model"]),
                "devname": c["devname"] or c["model"],
                "brandcn": c["brandcn"] or brand_cn(c["brand"], c["devname"]),
                "targets": targets_str(c),
                "optional": "1" if c["optional"] else "0",
                "referer": c["referer"],
                "url": c["url"],
                "resolve": c["resolve"],
                "stable": "1" if c["stable"] else "0",
            }
            for c in picked
        ]
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", default="devices.json")
    ap.add_argument("--out", default="out/candidates.json")
    ap.add_argument("--matrix-out", default="")
    ap.add_argument("--state", default="state/seen.json")
    ap.add_argument("--cache-dir", default="state/cache")
    ap.add_argument("--only", default="", help="逗号分隔的 device id 白名单")
    ap.add_argument("--brand", default="", help="逗号分隔的 brand 白名单（adapter 名）")
    ap.add_argument("--brandcn", default="",
                    help="品牌中文名过滤：小米/红米/POCO/一加/OPPO/真我/魅族/全部")
    ap.add_argument("--max-new-per-device", type=int, default=3,
                    help="每个机型本次最多入队几个新版本（控制 Actions 额度）")
    ap.add_argument("--new-only", action="store_true")
    ap.add_argument("--all-versions", action="store_true",
                    help="对账模式：不看去重表，把所有版本都扫出来（配合 --existing 补漏）")
    ap.add_argument("--max-total", type=int, default=MAX_MATRIX,
                    help="本次最多派发多少个 job（对账模式务必调小，否则一次几百个 8G 包）")
    ap.add_argument("--existing", default="",
                    help="网盘现有文件清单（rclone lsf 的输出），已存在的不再派发")
    ap.add_argument("--sleep", type=float, default=1.0,
                    help="请求间隔秒，别把对方站点打疼")
    ap.add_argument("--print", dest="do_print", action="store_true")
    ap.add_argument("--emit-matrix", action="store_true")
    args = ap.parse_args()

    dev_path = Path(args.devices)
    spec = json.loads(dev_path.read_text("utf-8-sig"))
    devices = spec["devices"]

    seen = load_seen(Path(args.state))
    ctx = {
        "sleep": args.sleep,
        "base_dir": str(dev_path.parent),
        "cache_dir": args.cache_dir,
        "seen": seen,
        "max_new_per_device": args.max_new_per_device,
    }

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    brands = {s.strip() for s in args.brand.split(",") if s.strip()}
    if only:
        # 支持 id / 代号 / 中文名 模糊匹配：输入「一加15」也能命中「一加 15」
        def _norm(s):
            return re.sub(r"\s+", "", str(s)).lower()

        def _hit(d, keys):
            hay = _norm("".join(str(d.get(k, "")) for k in
                               ("id", "model", "display", "label")))
            return any(_norm(k) in hay for k in keys)
        devices = [d for d in devices if _hit(d, only)]
    if brands:
        devices = [d for d in devices if d.get("brand") in brands]
    if args.brandcn:
        want = {s.strip() for s in args.brandcn.split(",") if s.strip()} - {"全部"}
        if want:
            devices = [
                d for d in devices
                if (d.get("brandcn")
                    or brand_cn(d.get("brand", ""), d.get("display", ""))) in want
            ]
    if not devices:
        print("[warn] 机型表筛选后为空", file=sys.stderr)

    all_c: list[dict] = []
    for dev in devices:
        fn = ADAPTERS.get(dev.get("brand", ""))
        if not fn:
            print(f"[skip] {dev['id']}: 未知 brand {dev.get('brand')!r}",
                  file=sys.stderr)
            continue
        print(f"[scan] {dev['id']} ({dev.get('brand')}/{dev.get('model')})",
              file=sys.stderr)
        try:
            got = fn(dev, ctx) or []
        except Exception as e:  # noqa: BLE001
            print(f"  [fail] {dev['id']}: {e}", file=sys.stderr)
            continue

        if args.all_versions:
            fresh = got
        else:
            fresh = [c for c in got if not args.new_only or dedupe_key(c) not in seen]
        print(f"  -> {len(got)} found, {len(fresh)} new", file=sys.stderr)
        all_c.extend(fresh)

    # --- 对账：网盘上已有的不再派发 ---
    if args.existing:
        have = load_existing(Path(args.existing))
        before = len(all_c)
        all_c = [c for c in all_c if dest_rel(c) not in have]
        print(f"[reconcile] 网盘已有 {before - len(all_c)} 个，待补 {len(all_c)} 个",
              file=sys.stderr)

    # --- matrix ---
    matrix = build_matrix(all_c, args.max_total)
    ready = [c for c in all_c if c["status"] == "ready" and c["url"]]
    if args.matrix_out:
        mp = Path(args.matrix_out)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(matrix, ensure_ascii=False), "utf-8")
    if args.emit_matrix:
        print(json.dumps(matrix, ensure_ascii=False))

    pending = sorted({c["device_id"] for c in all_c if not dispatchable(c)})
    if pending:
        print(f"[note] 抓到了但没派发（缺直链/缺解析器）: {pending}", file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"generated": time.strftime("%FT%TZ"), "items": all_c},
                   ensure_ascii=False, indent=2),
        "utf-8",
    )

    if args.do_print:
        for c in all_c:
            mark = "定" if c["stable"] else "动"
            flag = "" if c["status"] == "ready" else f"  [{c['status']}]"
            print(f"[{mark}] {c['brand']:<8} {c['model']:<12} {c['version']:<22} "
                  f"{c['filename']:<50} {c['url']}{flag}")

    # 只有「拿到直链」的才记入 seen。
    # 动态链接（resolve）不记：它每天都要重新查，靠 farm 端的同目录查重避免重复上传。
    if not args.all_versions:
        for c in all_c:
            if c["status"] == "ready" and c["url"]:
                seen.add(dedupe_key(c))
    state_path = Path(args.state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"keys": sorted(seen)}, ensure_ascii=False, indent=1), "utf-8"
    )

    n_ready = sum(1 for c in all_c if c["status"] == "ready")
    print(f"\n[done] {len(all_c)} candidates ({n_ready} ready, "
          f"{len(all_c) - n_ready} resolve) -> {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
