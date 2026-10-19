# boot-farm

给 xxrom.cn 用的批量取 boot 流水线：**机型表 → 自动找该机型各版本刷机包直链 → 下载 →
提取 boot/init_boot/vbmeta → 加密打包 → 上传 123 网盘 → 生成站点索引**。

原来是「一次 `workflow_dispatch` 喂一个 `rom_url`」，现在改成「加一行机型，机器自己排期」。

```
devices.json ──► discover.py ──► matrix ──► extract_one.sh (×N 并发) ──► 123 网盘
 (机型表)        (各版本直链)      (待办)      (下载/提取/打包/上传)         │
                                                                          ▼
                                                            make_index.py → data/index.json
```

## 目录

| 文件 | 作用 |
|---|---|
| `devices.json` | **机型表，你真正要维护的东西**。加一行 = 多一个自动抓取目标 |
| `discover.py` | 多源发现器，零依赖。找机型全部版本 → 出新版本的直链 |
| `extract_one.sh` | 单包：下载 → 提 boot → 7z 加密 → rclone 上传 123 |
| `make_index.py` | 扫 123 目录，生成 `index.json` 给站点前端用 |
| `gen_devices.py` | 从站点地图批量生成机型表骨架（几百个机型一次到位） |
| `selftest.py` | 离线自检，33 项，不联网 |
| `links.txt` | 手工链接队列（人肉找到的链接也能走同一条流水线） |
| `.github/workflows/batch.yml` | 主 workflow：发现 → 并发提取 → 重建索引 |
| `.github/workflows/selftest.yml` | 自检 workflow：push 自动跑离线自检，手动可实时探测源站 |

## 先跑一遍（不用等定时任务）

push 上去以后有两条路验证，都不需要配 123 的 secret：

| 想验证什么 | 怎么跑 | 会不会下载 |
|---|---|---|
| 代码有没有坏 | push 即触发 `selftest` 的 offline job（30 秒） | 不下载、不联网 |
| 源站能不能抓到 | Actions → `selftest` → Run workflow，`device` 填 `xiaomi-15` | 只发现，不下载 |
| 欧加解析工具能不能用 | Actions → `selftest` → Run workflow，`brand` 填 `oplus` | 只解析出链接给你看，不下载 |
| 全流程（含 123 上传） | Actions → `batch-boot` → Run workflow，`dry_run=true` | 不下载，只看清单 |
| 真的提包 | `batch-boot`，`dry_run=false` | 下载 + 提取 + 上传 |

## 部署（5 步）

1. 新建仓库（或直接用 `sake1227/boot`），把下面这些放到**仓库根目录**：

   ```
   devices.json  discover.py  extract_one.sh  make_index.py  gen_devices.py
   selftest.py   links.txt    获取解压密码.txt
   .github/workflows/batch.yml
   ```

2. 配两个 secret（Settings → Secrets and variables → Actions）：

   | Secret | 内容 |
   |---|---|
   | `RCLONE_CONF` | rclone.conf 全文（webdav remote 名叫 `123`，指向 123 网盘） |
   | `UNZIP_PASSWORD` | 压缩包密码 |

   你仓库里如果已经有 `rclone.zip`，workflow 会兜底用它，不必配 `RCLONE_CONF`。

3. 改 `devices.json`，先只留你要做的几个机型。

4. 手动跑一次 **dry run**：Actions → `batch-boot` → Run workflow →
   `dry_run = true`。这一步只发现链接、不下载，用来确认源站能抓到东西。

5. 确认 `out/matrix.json` 里有东西后，正式跑（`dry_run = false`）。之后每天
   北京时间 02:00 自动增量跑。

## 各品牌怎么配

| brand | model 填什么 | 一次能拿到 | 备注 |
|---|---|---|---|
| `xiaomi` | 机型代号 `dada`（小米15）、`houji`（小米14） | **该机型全部历史版本**，默认一版一个卡刷包 | 走 miuiver 索引页取版本列表，详情页里的官方 CDN 直链（`*.aliyuncs.com`）永久有效；`adapter_args.kinds` 可加 `images`（线刷包），`ota_incremental` 差分包含自动跳过 |
| `google` | Pixel 代号 `shiba` / `husky` / `akita` | factory + OTA 全量 | `adapter_args.kind = factory` 或 `ota` |
| `samsung` | `SM-S9110`，`region` 填 CSC 如 `CHC` | 最新版本号（免费） | 包体走 FUS 认证，标记为 `resolve` 不派发；要真下载得接 samloader |
| `lolinet` | `XT2401` 之类，配合 `root`/`match` | 该机型所有镜像 | h5ai 目录索引，永久直链，适合铺存量 |
| `oplus` | OTA 代号 `PJD110` / `RMX3301`，`region` 填 `cn` 等 | 最新全量包（灰度/预览可开） | 动态链接，**在下载 job 里现场解析**，不写去重表 |
| `manual` | `links.txt` | 你人肉找的链接 | 过渡用，一样走完整流水线 |
| `cmd` | 任意 shell 命令 | stdout 里的 URL | **逃生口**：OPPO/一加/realme 用这个包社区工具 |

小米各区域代号后缀：`cn` 无后缀，`global`/`eea`/`in`/`id`/`ru`/`tr`/`tw`/`jp`/`kr`/`sg` 对应
`-global`/`-eea`/…；`adapter_args.rom_type` 可选 `stable` / `developer`。

### 欧加（OPPO / 一加 / realme）

这类机型**没有永久直链**：查询接口只给一个 `downloadCheck?` 动态 URL，10~30 分钟过期。
所以 `oplus` adapter **不在发现阶段解析链接**，只把一条 `resolve` 命令塞进 matrix，
由 `extract_one.sh` 在下载的同一个 job 里「解析完立刻下」：

```
discover: python3 tools/oplus/tomboy_pro.py PJD110 cn --anti 1 --mode taste --original_link 1
              ↓ 只写进 matrix，不执行
farm:      bash -c "<那条命令>" → 抓 .zip/.ozip → 重试 3 次 → 立刻 aria2 下
```

配置就一行，`model` 填机型代号（`getprop ro.oplus.version` 里 `_11.` 前面那段）：

```json
{ "id": "oneplus-13-cn", "brand": "oplus", "model": "PJD110", "region": "cn" }
```

`tomboy_pro.py` 会自动补 `_11.A / _11.C / _11.F / _11.H / _11.J` 后缀。

要把某个机型的多个历史版本一次跑完，用 `versions` 列表（版本号自己贴进去即可）：

```json
{
  "id": "realme-gt-cn",
  "brand": "oplus",
  "model": "RMX3301",
  "region": "cn",
  "label": "realme-GT",
  "adapter_args": {
    "versions": ["RMX3301_11.H.10_1010", "RMX3301_11.H.12_1210"],
    "extra_flags": "--anti 1 --mode taste"
  }
}
```

- `label` 只影响 123 网盘的目录名（`123:zhanfang/oplus/realme-GT/`）。
- `extra_flags` 是透传给 `tomboy_pro.py` 的查询开关，灰度/预览版改这里。
- 工具不打包进仓库，workflow 每次 `git clone --depth 1` 上游最新版到 `tools/oplus`。
- 因为链接会过期，**动态源不写去重表**；重复运行靠 farm 端「上传前查同目录有没有同名
  zip」来跳过，不会重复上传也不会重复下载。

社区实现（这些是 `oplus` adapter 背后的工具，公开仓库）：

- OPPO / 一加 / realme：[SunsetMkt/OPlus-Tracker](https://github.com/SunsetMkt/OPlus-Tracker)
- 小米官方 OTA 接口（想绕开索引站时用）：[YuKongA/Updater](https://github.com/YuKongA/Updater)
- 三星 FUS：[samloader-fus](https://docs.rs/samloader-fus)

## 提哪些分区

`extract_one.sh` 默认 `PARTITIONS=boot,init_boot,vbmeta`，要加就改 workflow 里的环境变量
（`vendor_boot`、`dtbo` 都可以）。**只解需要的分区**是硬要求：完整解一个 8G 的 super
会直接撑爆 GitHub 的 14G 盘。

支持的包类型：

| 包 | 处理方式 |
|---|---|
| 小米卡刷包 `.zip`（内含 `payload.bin`） | `payload_dumper --partitions …` |
| 小米线刷包 `.tgz`、三星 `.tar.md5` | `tar` 展开后按文件名找 `boot.img` |
| Google factory/OTA、lolinet `.zip` | 展开后找 `boot.img` |
| `*.img.lz4` | 自动 `lz4 -d` |
| `.ozip` | 不支持，需先解密，脚本会明确报错退出 |

## 索引给站点用

`index` job 会把 `data/index.json` 提交回仓库：

```json
{ "devices": { "xiaomi": { "dada": [
  { "name": "dada-ota_full-OS3.0.307.0.WOCCNXM-user-16.0-6703365c83",
    "zip": "xiaomi/dada/xxx.zip", "bytes": 6500000000, "size": "6.1 GB",
    "mtime": "2026-09-01 10:00:00",
    "images": ["boot.img","init_boot.img","vbmeta.img"],
    "version": "3.0.307", "source_url": "https://bkt-...aliyuncs.com/..." } ] } } }
```

`zip` 是 123 网盘里的相对路径，配合你的分享前缀就是下载地址。国内访问
`raw.githubusercontent.com` 不稳的话，用 jsDelivr 拉这个 json。

## 额度与并发（务必看一眼）

- 单个小米卡刷包 6~8G，3 个并发大概吃掉 20 分钟 job 时间/轮。
- workflow 默认 `max_new_per_device: 1`、`max-parallel: 3`。想一次性把存量铺完，
  把 `max_new_per_device` 调大（比如 20），让某个机型单独跑一轮。
- Public 仓库 Actions 分钟数不计数，但**单 job 上限 6 小时**、matrix 上限 256 个 job。
  真跑量建议上自建 runner：一台 4C8G / 200G 轻量云，没有磁盘和时长限制。
- 123 网盘上传并发太高会被限速，`max-parallel` 别超过 3~4。

## 机型表和版本清单：roms.lian86.top（RomCloud）

这个站解决了欧加最麻烦的两件事，但不是全部：

| 能力 | 能不能自动拿 | 说明 |
|---|---|---|
| 机型名 ↔ OTA 代号 | ✅ 能 | 品牌页 `/brand/OnePlus`、`/brand/OPPO`、`/brand/Realme` 全部列出 |
| 某机型的**全部历史版本号** | ✅ 能 | 机型页 `/brand/OnePlus/model/PJZ110` 列出该机型所有版本（一加13 有 35 个） |
| 版本的**下载链接** | ❌ 不能 | 版本页要求「完成一次人机验证后即可获取该版本下载链接」，这是人家刻意的门禁，不该绕 |

所以分工是：**RomCloud 当目录，`tomboy_pro.py` 当链接来源。**

```bash
# 抓一加/OPPO/realme 全部机型代号 → 298 个（一加 44 / OPPO 167 / realme 87）
python gen_devices.py --source romcloud --brand OnePlus,OPPO,Realme -o devices.oplus.json
```

生成结果在 `devices.oplus.json`，可以直接挑需要的机型抄进 `devices.json`。
`display` 是中文机型名，`model` 是代号（`PJZ110` 一加13、`PLK110` 一加15、`PYB110` 一加16）。

**不要直接把 298 个全丢进 `devices.json`**：每个机型要查 5 个分支，一天就是 1490 次 OTA
请求，既慢又打人家服务器。按你站上真正有流量的机型维护那十几到几十个就够。

顺带一个用法：如果你想知道某机型到底出过哪些版本（哪怕拿不到链接），直接抓机型页即可，
版本号就写在 URL 里：

```bash
curl -s https://roms.lian86.top/brand/OnePlus/model/PJZ110 \
  | grep -oE 'PJZ110_[0-9.]+\(CN[0-9]+\)' | sort -u
```

拿到 `16.0.5.702(CN01)` 这种版本号后，可以反向确认 `tomboy_pro.py --components` 该查哪个增量包。

## 验证状态

| 环节 | 状态 |
|---|---|
| 小米全链路（索引页 → 版本列表 → 官方直链） | **已实测通过**。本机实跑 `xiaomi-15 / xiaomi-14 / redmi-k80`，拿到 `dada-ota_full-OS3.0.307.0.WOCCNXM-...zip` 等真实直链，Range 请求返回 HTTP 206 |
| 增量包过滤 | 已实测。修掉了一个真 bug：一个版本会同时挂 `ota_full` / `ota_incremental` / `images`，增量包是差分 payload，解不出完整 boot |
| 欧加 OTA 查询 | **已实测通过**。13 个国内代号、35 条版本记录全部拿到真实链接（见上一节表格） |
| 欧加直链可下载性 | **已实测**：带签名链接 HTTP 206（8.18G 真包，`PK` 头正常），无签名原始链接 403 |
| Google / lolinet / 三星 | 本机到这三个源的网络被拦（Google 超时、lolinet 证书校验失败、ospserver 403），**在 GitHub runner 上通常会好**，但没实测过 |

## 欧加国内机型实测结果（真跑过，不是推测）

`tomboy_pro.py` 一次依次查 `_11.A / _11.C / _11.F / _11.H / _11.J` 五个分支，
每个分支给出该分支**最新一个完整包**。所以「各版本」= 每个 ColorOS 大版本各一个包。

| 代号 | 型号（参考） | 拿到的版本（旧 → 新） |
|---|---|---|
| `NE2210` | 一加 10 Pro | `11_A.19` → `13.1.0.182` → `14.0.0.500` → `15.0.0.1301` → `16.0.3.530` |
| `PHB110` | 一加 11 | `13.1.0.196` → `14.0.0.114` → `15.0.0.861` → `16.0.5.1002` |
| `PJA110` | 一加 Ace 2 Pro | `13.1.1.552` → `14.0.0.500` → `15.0.0.861` → `16.0.5.1201` |
| `PJD110` | 一加 12 | `14.0.0.503` → `15.0.0.860` → `16.0.10.501` |
| `PJE110` | 一加 Ace 3 | `14.0.0.317` → `15.0.0.870` → `16.0.5.1002` |
| `PJX110` | 一加 Ace 3 Pro | `15.0.0.861` → `16.0.5.1001` |
| `PJZ110` | 一加 13 | `15.0.0.861` → `16.0.10.501` → `17.0.0.103` |
| `PLA110` | OPPO Reno14 | `15.0.2.610` → `16.0.10.600` |
| `PHZ110` | OPPO Find X7 | `14.0.1.410` → `15.0.0.860` → `16.0.10.500` |
| `PJT110` | OPPO Find X8 | `15.0.0.1360` → `16.0.5.1200` |
| `RMX3820` | realme GT5 | `13.1.1.148` → `14.0.0.211` |
| `RMX3706` | realme GT Neo5 | `13.1.0.118` → `14.0.0.100` → `16.0.5.1000` |
| `RMX5200` | realme（待核对） | `16.0.9.402` → `17.0.0.106` |

型号名是按公开资料对的，可能有偏差，**以代号为准**。这 13 个已经写进 `devices.json`。
`PJX110=Ace 3 Pro`、`PLA110=Reno14`、`PJD110=一加12` 有零售页/固件站页面作旁证。

实测确认的两个坑，脚本已处理：

1. **同一机型两个链接，一个能下一个 403。** 不带 `sign=`/`Expires=` 的
   `gauss-componentotacostmanual-cn...` 原始链接直接 403；带签名的
   `gauss-compota-c-cn...&Expires=...&Signature=...` 才能下。`extract_one.sh` 现在优先选带签名的。
2. **不是每个分支都给完整包。** `RMX3706_11.H` 只给 88MB（增量/组件包），
   `PHB110_11.H` 给 8.18G（真完整包）。`extract_one.sh` 加了体积预检，小于 1G 直接跳过并在
   job 摘要留一行说明，不会白解压。

## 已知限制

1. **欧加只拿得到「每个分支的最新包」。** OPPO/一加/realme 没有公开的版本枚举接口，同一分支里的中间小版本（比如 `16.0.5.1000` 之后又出的 `16.0.5.1001`）只能靠 `adapter_args.versions` 贴版本号逐个查。
2. 小米这边如果 miuiver 改了 URL 规则（`/dada-v2-0-216/` 这种），要改 `ad_xiaomi` 里那条正则。
   改 HTML 模板不影响。
3. 小米部分老版本只有线刷包、且需要登录，抓不到直链会打 warning 跳过。
4. `lolinet` 的 `root` / `match` 要按你要的机型改，`devices.json` 里那条是示例。
5. 三星目前只查到版本号（`resolve` 但不派发），要真下载得自己接 samloader。

miuiver 是第三方索引站，脚本已做礼貌化处理：每天一轮、请求间隔 1s、详情页缓存 7 天、
已知版本一次请求都不发。**实测一轮 3 个机型只发了 6 个请求**。如果你想直接打厂商接口，
把 `ad_xiaomi` 换成 `cmd` 适配器包 Updater 即可。

## 本地验证

```bash
python selftest.py                                     # 33 项离线自检
python gen_devices.py -o devices.generated.json        # 生成机型表骨架
python discover.py --devices devices.json --only xiaomi-15 --print   # 只扫一个机型
python make_index.py listing.txt -o index.json         # 试跑索引
```
