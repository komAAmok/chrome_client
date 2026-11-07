# Chrome 86–98：补齐说明与证据

这一批 profile 不在仓库原有范围内（原为 `chrome_99`–`chrome_157`，59 个）。
本文记录它们从哪来、凭什么对齐、以及哪些结论**不能**下。

## 1. 数据来源与事实基线

指纹基准目录是**本仓库之外**的参考抓包集，由 `REFERENCE_CAPTURES_DIR` 指定；它提供
三份抓包，每份覆盖 86–157 共 72 个主版本、每版本 6 次独立连接：

| 文件 | 目标端点 | 覆盖 | 可用样本 |
| --- | --- | --- | --- |
| `tls-results.json` | `https://tls.peet.ws/api/all`（H2） | 86–157 | 432/432 |
| `tls-results-h1.json` | 同上（H1） | 86–157 | 432/432 |
| `tls-results-h3.json` | `https://quic.tools.scrapfly.io/api/fp/quic`（H3） | 86–157 | 390/432 |

**H3 缺口（必须显式声明）**：86、87、88、89、90、91、92 七个版本在基准中 6 次全部
失败，错误一致为 `page.goto: net::ERR_QUIC_PROTOCOL_ERROR`。这是基准本身的缺失，
不是实现偏差——本仓库无法用该目录证明这 7 个版本的 H3 指纹。见第 5 节。

## 2. 逐版本对齐依据：每个版本用**自己的** tag 源码

硬性要求是「以对应版本的 h1-h3 tag 源码为准」。`tools/collect-profile-evidence.py`
原本只为 99+ 服务，其中的 12 个审计文件路径在更早的版本上并不存在。实测得到的
历史分界（不是推测，是逐个 tag 抓取 404/200 得到的）：

| 版本 | BoringSSL 扩展实现 | BoringSSL 随机数 | QUICHE 配置 | QUICHE 随机数 |
| --- | --- | --- | --- | --- |
| 86–92 | `ssl/t1_lib.cc`（尚未拆出 `extensions.cc`） | `crypto/fipsmodule/rand/rand.c` | `quic/core/quic_config.cc` | `quic/core/crypto/quic_random.h` |
| 93–151 | `ssl/extensions.cc` | `crypto/fipsmodule/rand/rand.c` | `quic/core/quic_config.cc` | `quic/core/crypto/quic_random.h` |
| 152–157 | `ssl/extensions.cc` | `crypto/rand/rand.cc` | `quiche/quic/core/quic_config.cc` | `quiche/common/quiche_random.cc` |

采集器新增 `PATH_ERAS` 表按此解析（`tools/collect-profile-evidence.py`），因此
每个版本的证据指向的是该 tag 真实存在的文件，而不是一个 99+ 才有的路径 404 后
被静默跳过。

## 3. 关键版本边界（全部由抓包与源码双向确认）

| 事实 | 86–90 | 91–98 | 依据 |
| --- | --- | --- | --- |
| ALPS 扩展 | 无 | 有（旧码点 17513） | `net/base/features.cc` 在 91 才定义 `kAlpsForHttp2`；`http_network_session.cc` 同期才出现 `application_settings_[kProtoHTTP2]` |
| ECH | 无 | 无 | 抓包 86–118 均无 65037 |
| trust_anchors (0xca34) | 无 | 无 | 152 才出现 |
| ClientHello padding 长度 | 418 | 400（91–97）/408（98） | 6 次采样内稳定 |
| 扩展顺序 | 原生（固定） | 原生（固定） | 6 次采样只有 1 种顺序；随机洗牌从 111 开始 |
| key_share 数量 | 1 | 1 | 124 才升到 2 |
| H2 `ENABLE_PUSH` | 不发 | 不发 | 106 才开始发 |
| H2 `MAX_CONCURRENT_STREAMS` | 发 | 发 | 117 才停发 |

## 4. 生成方式：从各自证据推导，不继承模板

`tools/build-profile-entry.py` 是**从模板继承**再断言模板值仍成立的，它的模板是
chrome_153。用它去造 chrome_86 就是把 99+ 的常量倒推回去，正是「不得跨版本混用、
近似替代」所禁止的。因此新增 `tools/derive-profile-from-capture.py`：

它的每一个值都只来自该版本自己的抓包或自己的 tag 源码，并在写出前做双向交叉校验：

- **wire ↔ source**：抓包里的扩展集必须能被该 tag 审计到的行解释；ALPS 码点必须与该 tag
  在 `net/base/features.cc` 里真实定义的 feature 一致；trust_anchors/ECH/padding 同理。
  不一致是硬失败，不是警告。
- **wire ↔ wire**：稳定值必须在 6 次连接上完全一致，否则它就不是稳定值。

**校准**：用同一工具重新推导已审计的 `chrome_99`，与仓库中已提交的条目逐字段比较：
`tls`（含 cipher/curve/signature/extension 顺序/GREASE/key_share）、`evidence.source_files`
全部一致。这证明推导方法与仓库既有 profile（当时 59 个，加入本批后 72 个）的口径相同。
## 5. 未解决项与不可下达成的结论

### 5.1 H3 在 86–92：明确不支持（fail-closed）

基准 `tls-results-h3.json` 中 86–92 各 6 次全部 `net::ERR_QUIC_PROTOCOL_ERROR`，零可用样本，
因此这 7 个版本的 HTTP/3 行为**没有可复现的参照**。

处置不是「跳过」，而是**明确不支持并在引擎层拒绝**：

- `MN_PROTOCOL_FORCE_H3` 在这些 profile 上返回 `MN_ERROR_PROFILE_UNSUPPORTED`；
- `activation.h3_unsupported = true`，`h3_unsupported_reason` 写明基准缺失；
- `tools/audit-h3-profile-support.py` 直接读生成表断言「不支持集」恰为 86–92。

修复前实测：chrome_86 强制 H3 会真的发出 QUIC 并拿到 HTTP 200，等于报告了该版本不存在的
指纹。那是缺陷，现在是拒绝。

**注意区分**：`activation.h3_disabled` 是另一个事实（「该版本不默认尝试 QUIC」，
86–122 为 true），与「能否做 H3」无关。两者曾被混用，导致 93–122 被错误当成不具备 H3 能力，
而实际上每版都有 6 次可用 H3 抓包且对齐通过（93–98 见 §5.1 更正）。

### 5.2 HTTP/1.1 默认请求头不由 profile 决定

基准的 H1 记录是**浏览器导航请求**的头集（`sec-ch-ua`、`Sec-Fetch-*`、`Accept-Encoding: zstd`
等 11–13 个头）。本 Core 的 `URLRequest` 发出的是 API 客户端请求（Host/Connection/
User-Agent/Accept-Encoding 4 个头）。这是 `docs/COMPATIBILITY_BOUNDARY.md` 已声明的边界
（「默认请求头不是 profile 数据」），不是这批 profile 引入的偏差。
**影响**：H1 日志的 `http1_headers` 记为 OBSERVED 而非断言 PASS，原因写入每条日志。

### 5.3 User-Agent 的平台令牌随宿主机，而非基准的 Windows

版本号来自 profile（86–157 各自真实 tag 版本串）；平台令牌按 `BUILDFLAG(IS_WIN/IS_MAC)`
与 `ARCH_CPU_*` 跟随本机，因此 Linux 宿主发 `X11; Linux x86_64`，基准是 Windows。
**影响**：UA 只断言 `Chrome/<version>` 令牌一致，完整串记为 OBSERVED。

### 5.4 顺带修正的既有缺陷（不是本任务引入）

| 缺陷 | 位置 | 证据 |
| --- | --- | --- |
| `HistoricalUserAgent` 只识别 `major >= 99`，86–98 退回 `minicronet/<ver>` | `core/source/minicronet.cc` | 修复前 chrome_86 实测 UA 为 `minicronet/0.2.8` |
| 同上，版本表只覆盖 99–104，105+ 一律 `<major>.0.0.0` | 同上 | 基准里 105–157 都是四段真实版本 |
| 生成的 chrome_148 `try_quic_by_default` 与提交的表不一致 | `core/source/minicronet/profile_table_generated.h` | 用 HEAD 输入 + HEAD 生成器重算得 `true`，提交值是 `false`；同一生成器 `assert_span` 要求 ≥123 为 true |
| `verify-wire-capture.py` 无条件要求 `ech_digest` 随机 | `tools/verify-wire-capture.py` | 不发 ECH 的版本恒得 1 个 unique 被判失败；已改为仅当扩展存在时检查，并用 152/86 双向验证 |

## 6. 逐版本证据目录

每个版本一个目录，三个协议各一个子目录，全部 72 个版本形状一致：

```text
profiles/chrome_<major>/
  meta.json            版本、抓包端点、每个协议的完整性与连接数
  source-evidence.json 该 tag 的 12 个审计文件：路径/字节/sha256/信号行号
  README.md            该版本的说明与复现命令
  h2/captures.json     6 条连接，端点原始响应，拼接的 JSON 对象（不是数组）
  h2/samples.json      同样 6 条，外加 checks：ja3/ja4/peetprint、session id、client random
  h1/  h3/             同构；86–92 的 h3 目录是 unsupported.json（附失败原因）
```

`captures.json` 之所以是拼接对象而不是数组：wire gate 就是按「一条连接一个对象」解析的，
存成数组会被读成单条记录，所有随机性检查都会空转通过。

要单看某一条连接的原文：

```bash
python3 -c "import sys;sys.path.insert(0,'tools');import profile_captures as pc;r=pc.load_protocol(86,'h2')[0];import json;print(json.dumps(r,ensure_ascii=False,indent=2)[:2000])"
```
