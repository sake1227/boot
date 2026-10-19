#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线自检 —— 不联网，用固定夹具验证各 adapter。

    python selftest.py

重点验证两件事：
1. 解析器不依赖 HTML 标签 —— 同一份数据用两种截然不同的标记结构喂进去，结果必须一致。
2. 动态链接（欧加）不在 discover 阶段解析，只带 resolve 命令进 matrix，由 farm 现场解析。
"""

import json
import sys

import discover as d

# --- 同一批数据的两种标记变体 ------------------------------------------------- #

# 变体 1：table + 绝对链接 + title 属性（最接近真实页面）
TAG_A = """
<html><body><h1>小米15 正式版刷机包下载合集</h1>
<table>
<tr><th>设备名称</th><th>固件版本</th><th>查看详情</th></tr>
<tr><td>小米15</td><td>OS1.0.1.0.UNC CNXM</td><td>
  <a href="https://miuiver.com/dada-v1-0-1/" title="小米15 V1.0.1">前往下载</a></td></tr>
<tr><td>小米15</td><td>OS3.0.307.0.WOCCNXM</td><td>
  <a href="https://miuiver.com/dada-v3-0-307/" title="小米15 V3.0.307">前往下载</a></td></tr>
<tr><td>小米15</td><td>OS2.0.216.0.VOCCNXM</td><td>
  <a href="https://miuiver.com/dada-v2-0-216/" title="小米15 V2.0.216">前往下载</a></td></tr>
</table></body></html>
"""

# 变体 2：ul/li + 参数在 href 前面 + 单引号 —— 完全不同的结构
TAG_B = """
<html><body><ul>
<li><a title='oldest' href='https://miuiver.com/dada2-v1-0-1/'>v1.0.1</a></li>
<li><a title='newest' href='https://miuiver.com/dada2-v3-0-307/'>v3.0.307</a></li>
<li><a title='mid' href='https://miuiver.com/dada2-v2-0-216/'>v2.0.216</a></li>
</ul></body></html>
"""

DETAIL_A = """
<html><body><h2>下载地址</h2>
<blockquote><p><a href="https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/OS3.0.307.0.WOCCNXM/dada-ota_full-OS3.0.307.0.WOCCNXM-user-16.0-6703365c83.zip">卡刷包下载</a></p></blockquote>
<blockquote><p><a href="https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/OS3.0.307.0.WOCCNXM/dada-ota_incremental-OS3.0.306.0.WOCCNXM-OS3.0.307.0.WOCCNXM-user-16.0-aaa111.zip">增量包下载</a></p></blockquote>
<p><a href="https://miuiver.com/faq/">常见问题</a>
   <img src="https://miuiver.com/wp-content/logo.png"></p>
</body></html>
"""

DETAIL_B = """
<html><body><div class="links">
<div><a rel="nofollow" href="https://bigota.d.miui.com/OS3.0.307.0.WOCCNXM/dada2_images_OS3.0.307.0.WOCCNXM_20260101.0000.00_16.0_cn_abc123.tgz">线刷包</a></div>
<a href="https://miuiver.com/login/">登录</a>
</div></body></html>
"""

GOOGLE = """
<html><body><table>
<tr><td>15.0.0 (AP4A.250105.002)</td>
    <td><a href="https://dl.google.com/dl/android/aosp/shiba-ap4a.250105.002-factory-1a2b3c4d.zip">Link</a></td></tr>
<tr><td>15.0.0</td>
    <td><a href="https://dl.google.com/dl/android/aosp/husky-ap4a.250105.002-factory-1a2b3c4d.zip">Link</a></td></tr>
</table></body></html>
"""

SAMSUNG = """
<?xml version="1.0" encoding="UTF-8"?>
<versioninfo>
  <url>https://fota-cloud-dn.ospserver.net/firmware/CHC/SM-S9110/version.xml</url>
  <upgrade><value rcount="3" fwsize="6000000000">
    <latest o="14">S9110ZCS3CXA1/S9110CHC3CXA1</latest>
    <latest o="15">S9110ZCS5DYA2/S9110CHC5DYA2</latest>
  </value></upgrade>
</versioninfo>
"""

LOLINET_ROOT = """
<html><body><table>
<tr><td><a href="/firmware/lenomola/2025/">2025</a></td></tr>
<tr><td><a href="https://mirrors.lolinet.com/firmware/lenomola/readme.txt">readme.txt</a></td></tr>
</table></body></html>
"""
LOLINET_2025 = """
<html><body><table>
<tr><td><a href="/firmware/lenomola/2025/XT2401-1/">XT2401-1</a></td></tr>
<tr><td><a href="/firmware/lenomola/2025/XT2301-2/">XT2301-2</a></td></tr>
</table></body></html>
"""
LOLINET_DEV = """
<html><body><table>
<tr><td><a href="XT2401-1_RTDC31.Q3-42-19_subsidy-DEFAULT_regulatory.zip">fw.zip</a></td></tr>
</table></body></html>
"""
LOLINET_OTHER = """
<html><body><table>
<tr><td><a href="XT2301-2_OTHER.zip">other.zip</a></td></tr>
</table></body></html>
"""

FIXTURES = {
    "https://miuiver.com/tag/dada-stable-rom/": TAG_A,
    "https://miuiver.com/dada-v1-0-1/": DETAIL_A,
    "https://miuiver.com/dada-v2-0-216/": DETAIL_A,
    "https://miuiver.com/dada-v3-0-307/": DETAIL_A,
    "https://miuiver.com/tag/dada2-stable-rom/": TAG_B,
    "https://miuiver.com/dada2-v1-0-1/": DETAIL_B,
    "https://miuiver.com/dada2-v2-0-216/": DETAIL_B,
    "https://miuiver.com/dada2-v3-0-307/": DETAIL_B,
    d.GOOGLE_IMAGES: GOOGLE,
    "https://fota-cloud-dn.ospserver.net/firmware/CHC/SM-S9110/version.xml": SAMSUNG,
    "https://mirrors.lolinet.com/firmware/lenomola/": LOLINET_ROOT,
    "https://mirrors.lolinet.com/firmware/lenomola/2025/": LOLINET_2025,
    "https://mirrors.lolinet.com/firmware/lenomola/2025/XT2401-1/": LOLINET_DEV,
    "https://mirrors.lolinet.com/firmware/lenomola/2025/XT2301-2/": LOLINET_OTHER,
}

CALLS: list[str] = []


def fake_fetch(url, timeout=40, retries=3):
    CALLS.append(url)
    if url in FIXTURES:
        return FIXTURES[url]
    raise RuntimeError(f"fixture missing: {url}")


def ctx(seen=None, maxn=2):
    return {"sleep": 0, "base_dir": ".", "cache_dir": "/tmp/nonexistent-cache",
            "seen": seen or set(), "max_new_per_device": maxn}


def main() -> int:
    d.fetch = fake_fetch
    d.fetch_cached = lambda url, c: fake_fetch(url)
    failures = []

    def check(name, cond, extra=""):
        print(f"{'PASS' if cond else 'FAIL'}  {name} {extra}")
        if not cond:
            failures.append(name)

    # --- 小米：变体 1 ---
    CALLS.clear()
    a = d.ad_xiaomi({"id": "xiaomi-15", "brand": "xiaomi", "model": "dada"}, ctx())
    check("小米V1 抓到 2 个版本(受 maxn 限制)", len(a) == 2, str(len(a)))
    check("小米V1 新版本优先", [c["version"] for c in a] == ["3.0.307", "2.0.216"],
          str([c["version"] for c in a]))
    check("小米V1 直链来自官方 CDN", all("aliyuncs.com" in c["url"] for c in a))
    check("小米V1 标为永久直链", all(c["stable"] for c in a))
    check("小米V1 只要 .zip 不要图片/页内链接",
          all(c["filename"].endswith(".zip") for c in a))
    check("小米V1 增量包被过滤（差分包解不出 boot）",
          all("incremental" not in c["filename"] for c in a),
          str([c["filename"][:30] for c in a]))
    check("小米V1 只抓了索引页+2个详情页", len(CALLS) == 3, str(len(CALLS)))

    # --- 小米：变体 2（不同标记，同数据）---
    CALLS.clear()
    b = d.ad_xiaomi({"id": "xiaomi-15b", "brand": "xiaomi", "model": "dada2",
                     "adapter_args": {"kinds": ["images"]}}, ctx())
    check("小米V2 换一套标记也能解析", len(b) == 2, str(len(b)))
    check("小米V2 版本顺序一致",
          [c["version"] for c in b] == ["3.0.307", "2.0.216"])
    check("小米V2 kinds=images 才拿 .tgz 线刷包",
          all(c["filename"].endswith(".tgz") for c in b))
    check("小米V2 tag 页只出现一次", sum(1 for u in CALLS if "/tag/" in u) == 1)

    # --- 小米：默认只要卡刷包，一版一个 ---
    check("默认 kinds 只收 ota_full",
          d.xiaomi_kind("x-ota_full-1.zip") == "ota_full" and
          d.xiaomi_kind("x-ota_incremental-1-2.zip") == "incremental" and
          d.xiaomi_kind("x_images_1.tgz") == "images")

    # --- 小米：去重（已抓过的版本不该再发详情页请求）---
    CALLS.clear()
    seen = {"xiaomi-15|v3.0.307|dada-ota_full-OS3.0.307.0.WOCCNXM-user-16.0-6703365c83.zip"}
    c = d.ad_xiaomi({"id": "xiaomi-15", "brand": "xiaomi", "model": "dada"},
                    ctx(seen=seen))
    check("已知版本被跳过", [x["version"] for x in c] == ["2.0.216", "1.0.1"],
          str([x["version"] for x in c]))
    check("跳过的版本不发详情页请求",
          "https://miuiver.com/dada-v3-0-307/" not in CALLS)

    # --- Google ---
    g = d.ad_google({"id": "pixel8", "brand": "google", "model": "shiba"}, ctx())
    check("Google 只命中匹配的 codename", len(g) == 1,
          g[0]["filename"] if g else "")
    check("Google 解析带 build 段的真实文件名",
          g and g[0]["filename"] == "shiba-ap4a.250105.002-factory-1a2b3c4d.zip")

    # --- 三星 ---
    s = d.ad_samsung({"id": "s23", "brand": "samsung", "model": "SM-S9110",
                      "region": "CHC"}, ctx())
    check("三星取 Android 版本最高的 latest",
          len(s) == 1 and "DYA2" in s[0]["version"],
          s[0]["version"] if s else "")
    check("三星标为待解析", s and s[0]["status"] == "resolve")
    check("三星没有 resolve 命令 -> 不派发", s and not d.dispatchable(s[0]))

    # --- lolinet ---
    m = d.ad_lolinet({"id": "moto", "brand": "lolinet", "model": "XT2401-1",
                      "adapter_args": {"root": "https://mirrors.lolinet.com/firmware/lenomola/",
                                       "max_depth": 3, "match": "XT2401"}}, ctx())
    check("lolinet BFS 命中 1 个 zip", len(m) == 1, m[0]["filename"] if m else "")
    check("lolinet 过滤掉其他机型", all("XT2301" not in x["url"] for x in m))

    # --- 欧加（OPPO/一加/realme）：动态链接，必须在 farm 里现场解析 ---
    o = d.ad_oplus({"id": "oneplus-13", "brand": "oplus", "model": "PJD110",
                    "region": "cn"}, ctx())
    check("欧加产出 1 条待解析任务", len(o) == 1, str(len(o)))
    check("欧加不预先解析直链（会过期）", o and o[0]["url"] == "")
    check("欧加带 tomboy resolve 命令",
          o and "tomboy_pro.py" in o[0]["resolve"] and "PJD110" in o[0]["resolve"],
          o[0]["resolve"] if o else "")
    check("欧加能进 matrix（farm 端解析）", o and d.dispatchable(o[0]) is True)
    check("欧加标为非永久直链", o and o[0]["stable"] is False)

    o2 = d.ad_oplus({"id": "realme-gt", "brand": "oplus", "model": "RMX3301",
                     "region": "cn",
                     "adapter_args": {"versions": ["RMX3301_11.H.10_1010"]}}, ctx())
    check("欧加 versions 模式逐个生成", len(o2) == 1 and
          "RMX3301_11.H.10_1010" in o2[0]["resolve"], o2[0]["resolve"] if o2 else "")
    check("欧加按 label 覆盖目录名",
          d.ad_oplus({"id": "x", "brand": "oplus", "model": "PJD110",
                      "label": "oneplus13", "region": "cn"}, ctx())[0]["model"]
          == "oneplus13")

    # --- matrix ---
    matrix = d.build_matrix(a + g + o + s + m)
    inc = matrix["include"]
    check("matrix 含直链项和 deferred 项",
          any(i["url"] and not i["resolve"] for i in inc) and
          any(i["resolve"] for i in inc), str(len(inc)))
    check("matrix 不含无法派发的三星", all(i["brand"] != "samsung" for i in inc))
    check("matrix 每项都有 name/resolve 字段",
          all("name" in i and "resolve" in i for i in inc))
    check("matrix 可 JSON 序列化", bool(json.dumps(matrix)))

    # --- 键稳定性 ---
    check("去重键形如 id|vX|file",
          a[0]["key"].startswith("xiaomi-15|v3.0.307|"), a[0]["key"])
    check("stem 去扩展名", d.stem_of("a.tar.md5") == "a" and d.stem_of("b.tgz") == "b")

    print()
    if failures:
        print(f"{len(failures)} 项失败: {failures}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
