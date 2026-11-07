# Chrome 86–157 支持矩阵与验收记录

记录时间：2026-11-07。所有数字都可按本文命令复现；没有一栏是推断出来的。

## 0. 结论

| 协议 | 结论 |
| --- | --- |
| HTTP/1.1 | **86–157 全部完整支持**（72/72，各 6 次完整请求对齐） |
| HTTP/2（native） | **86–157 全部完整支持**（72/72，各 6 次完整请求对齐） |
| HTTP/3 | **93–157 完整支持**（65/65）；**86–92 明确不支持**，强制 H3 时引擎拒绝 |

86–92 的「不支持」是**契约行为**，不是「未验证」：Core 在这些 profile 上创建 H3 引擎时
返回 `MN_ERROR_PROFILE_UNSUPPORTED`，不会退回 H2、也不会发出这些版本从未发过的 QUIC
ClientHello。修复前实测过一次：chrome_86 强制 H3 会真的发出 QUIC 并拿到 HTTP 200，等于
报告了一个该版本不存在的指纹——这正是 fail-closed 承诺要挡住的情况。

## 1. 验收口径

每个版本、每种协议跑 **6 次独立完整请求**（每次新进程，不复用连接/会话/套接字），
把端点返回的**完整响应体**落盘，再与 `完整/` 目录同版本基准抓包逐字段对齐。

| 类别 | 字段 | 判定 |
| --- | --- | --- |
| 稳定 | ciphers、extension_set、supported_groups、signature_algorithms、alpn、key_share_count、http2_settings（H2/H3） | 6 次全部等于基准 |
| 稳定 | h3_text、quic_version（仅 H3） | 同上 |
| 稳定 | user_agent 的 `Chrome/<version>` 令牌 | 同上（平台令牌随宿主，见 §4.3） |
| 随机 | client_random、session_id、key_share_values、destination_connection_id | 6 次**互不相同**；重复即判固定种子 |

单连接最多 3 次尝试，全部失败即该版本不可验收，不跳过。**全部 209 组 PASS 证据重试次数为 0。**

## 2. 支持矩阵

| Chrome | native (H2) | HTTP/1.1 | HTTP/3 |
| --- | --- | --- | --- |
| 86 | PASS | PASS | **不支持** |
| 87 | PASS | PASS | **不支持** |
| 88 | PASS | PASS | **不支持** |
| 89 | PASS | PASS | **不支持** |
| 90 | PASS | PASS | **不支持** |
| 91 | PASS | PASS | **不支持** |
| 92 | PASS | PASS | **不支持** |
| 93 | PASS | PASS | PASS |
| 94 | PASS | PASS | PASS |
| 95 | PASS | PASS | PASS |
| 96 | PASS | PASS | PASS |
| 97 | PASS | PASS | PASS |
| 98 | PASS | PASS | PASS |
| 99 | PASS | PASS | PASS |
| 100 | PASS | PASS | PASS |
| 101 | PASS | PASS | PASS |
| 102 | PASS | PASS | PASS |
| 103 | PASS | PASS | PASS |
| 104 | PASS | PASS | PASS |
| 105 | PASS | PASS | PASS |
| 106 | PASS | PASS | PASS |
| 107 | PASS | PASS | PASS |
| 108 | PASS | PASS | PASS |
| 109 | PASS | PASS | PASS |
| 110 | PASS | PASS | PASS |
| 111 | PASS | PASS | PASS |
| 112 | PASS | PASS | PASS |
| 113 | PASS | PASS | PASS |
| 114 | PASS | PASS | PASS |
| 115 | PASS | PASS | PASS |
| 116 | PASS | PASS | PASS |
| 117 | PASS | PASS | PASS |
| 118 | PASS | PASS | PASS |
| 119 | PASS | PASS | PASS |
| 120 | PASS | PASS | PASS |
| 121 | PASS | PASS | PASS |
| 122 | PASS | PASS | PASS |
| 123 | PASS | PASS | PASS |
| 124 | PASS | PASS | PASS |
| 125 | PASS | PASS | PASS |
| 126 | PASS | PASS | PASS |
| 127 | PASS | PASS | PASS |
| 128 | PASS | PASS | PASS |
| 129 | PASS | PASS | PASS |
| 130 | PASS | PASS | PASS |
| 131 | PASS | PASS | PASS |
| 132 | PASS | PASS | PASS |
| 133 | PASS | PASS | PASS |
| 134 | PASS | PASS | PASS |
| 135 | PASS | PASS | PASS |
| 136 | PASS | PASS | PASS |
| 137 | PASS | PASS | PASS |
| 138 | PASS | PASS | PASS |
| 139 | PASS | PASS | PASS |
| 140 | PASS | PASS | PASS |
| 141 | PASS | PASS | PASS |
| 142 | PASS | PASS | PASS |
| 143 | PASS | PASS | PASS |
| 144 | PASS | PASS | PASS |
| 145 | PASS | PASS | PASS |
| 146 | PASS | PASS | PASS |
| 147 | PASS | PASS | PASS |
| 148 | PASS | PASS | PASS |
| 149 | PASS | PASS | PASS |
| 150 | PASS | PASS | PASS |
| 151 | PASS | PASS | PASS |
| 152 | PASS | PASS | PASS |
| 153 | PASS | PASS | PASS |
| 154 | PASS | PASS | PASS |
| 155 | PASS | PASS | PASS |
| 156 | PASS | PASS | PASS |
| 157 | PASS | PASS | PASS |

统计：native 72/72、HTTP/1.1 72/72、HTTP/3 65/65 PASS，7 个不支持。

## 3. 证据与复现

```text
profiles/chrome_<major>/h2/captures.json   6 条连接，端点原始响应（native 协商）
profiles/chrome_<major>/h1/captures.json   同上，强制 HTTP/1.1
profiles/chrome_<major>/h3/captures.json   同上，强制 HTTP/3（86–92 为 unsupported.json）
profiles/chrome_<major>/<proto>/samples.json  同 6 条 + checks（ja3/ja4/peetprint、session、random）
```

每个 `captures.json` 是 6 个拼接的 JSON 对象，就是端点返回的原文，未经改写。
`tools/profile_captures.py` 是唯一读取它们的入口，四个内容门禁都经它取数。

我们自己的实现是否对齐，由 `tools/align-live-fingerprint.py` 现跑现比：它按协议选端点
（h1/h2 用 tls.peet.ws，h3 用 quic.tools），按协议选协议钉（h2 的参考抓包是 native
协商，钉 `h2` 会把 ALPN 收窄成 `[h2]` 并连带把 padding 从 418 推到 436），跑 6 次独立
进程，再逐字段与 `captures.json` 比对。`--offline` 只把参考与自身比一遍，用来证明比较器
本身没有把正确数据判错——209 组（72+72+65）全部 PASS。
86–92 没有 h3 抓包，取而代之的是 `profiles/chrome_<major>/h3/unsupported.json`，里面记录
了当时失败的原因（`ERR_QUIC_PROTOCOL_ERROR`）。这不是证据缺失：那三个版本确实做不出 QUIC
握手，契约是 Core 在这些 profile 上强制 H3 时**拒绝**，而不是发一个它们从未发过的 QUIC
ClientHello。`align-live-fingerprint.py` 对这类版本不比对指纹，而是跑一次引擎、断言它返回
`MN_ERROR_PROFILE_UNSUPPORTED`。

```bash
cargo build --offline -p minicronet --example fingerprint_probe

# h2 的参考抓包是 native 协商，所以钉 native（钉 h2 会把 ALPN 收窄、padding 从 418 变 436）
python3 tools/align-live-fingerprint.py --profile chrome_86 --protocol h2 --out /tmp/r86

# 86–92：没有抓包，断言引擎拒绝
python3 tools/align-live-fingerprint.py --profile chrome_86 --protocol h3

# 比较器自检：参考与自身比，209 组应全 PASS
python3 tools/align-live-fingerprint.py --profile chrome_86 --protocol h2 --offline

python3 tools/audit-h3-profile-support.py
```

## 4. 边界与不可下达成的结论

### 4.1 86–92 不支持 HTTP/3

依据：基准 `tls-results-h3.json` 中 86–92 各 6 次全部 `net::ERR_QUIC_PROTOCOL_ERROR`，
零可用样本，因此这些版本的 H3 行为**没有可复现的参照**。

- 契约：`MN_PROTOCOL_FORCE_H3` 在 86–92 上返回 `MN_ERROR_PROFILE_UNSUPPORTED`。
- 数据：`activation.h3_unsupported = true`，`h3_unsupported_reason` 写明原因。
- 门禁：`tools/audit-h3-profile-support.py` 直接读生成表，断言「不支持集」恰为 86–92；
  两个方向都做过变异测试（把 140 误标为不支持 → 失败；清除 86 标记 → 失败）。
- 注意：`activation.h3_disabled` 是**另一个**事实（「该版本不默认尝试 QUIC」，86–122 为 true），
  与「能否做 H3」无关。两者曾被混用，导致 93–122 被错误地当成不具备 H3 能力。

### 4.2 RFC 7685 padding：从「不可保证」到「机制已复现」

上一轮我把 119–121 的 padding 说成「与 ECH 载荷联动的双形态、无法保证」。这个说法是错的，
实测推翻了它：**120 次连续采样，padding 一次都没出现**。原因是确定性缺陷，不是随机性。

#### 根因（探针实测，已移除）

本仓库的 `profile-boringssl.patch` 用 `CBB_len(out)` 计算 ClientHello 长度。ECH 场景下该
缓冲区还挂着压缩暂存区：

```
body_len=103  ext=417  CBB_len(out)=522  →  msg_len=1071
```

真实长度只有 295。倍率把 `msg_len` 顶出 `(0xff, 0x200)` 窗口，padding 分支永远不执行。

#### 上游真实规则（从四个 tag 的 BoringSSL 直接读取）

| Chrome | BoringSSL | `client_hello_padding_length` 命中数 |
| --- | --- | --- |
| 119 | `d24a3820` | **0** |
| 120 | `c38dc298` | **0** |
| 121 | `1b7fdbd9` | **0** |
| 122 | `414f6950` | **0** |

该字段是本仓库补丁引入的，上游从来没有「固定 payload」这回事。上游只有一条规则：
`header_len` 落在 `(0xff, 0x200)` 时补到 512，`padding_len = 0x200 - header_len - 4`。
各版本报告的 418 / 400 / 408 是**各自 ClientHello 大小**的产物，不是配置值。

#### 修复

按上游同形实现：显式走 ClientHello body 结构（version/random/session_id/ciphers/compression）
得到真实 `header_len`，再套 512 规则。`client_hello_padding_length` 保留为「钉住总长」的
override，86–97 用它抵消本树 BoringSSL 与当年版本之间实测 8 字节的差异（记在
`PADDING_LENGTH_ERAS` 的注释里，是桥接不是复现）。

#### 验证

| 版本 | 基准 h2 | 实测 h2 | 基准 h1 | 实测 h1 |
| --- | --- | --- | --- | --- |
| 86 | 418 | **418** | 424 | **424** |
| 91 | 400 | **400** | 424 | **424** |
| 98 | 408 | **408** | 432 | **432** |
| 118 | 408 | **408** | 432 | **432** |

119–123 的触发条件也已实证：`padding ⟺ ECH GREASE 载荷 = 372 hex`，跨 119–123 无一例外；
实测 86 次采样中 `ech=372 → pad=28`、其余不补，与基准分布一致。

#### 验收口径：从"两种形态都接受"改为"按规则判定"

`padding_present` / `padding_data_length` 已加入必查字段（此前只被采集、从未断言，
这正是缺陷能藏在 6 行绿字后面的原因）。但只加字段不够——"两种形态都接受"同样会放行
一个 **padding 永不触发** 的实现，而这正是本仓库此前的真实故障。

实测把规则钉死了：

| 区间 | 行为 | 依据 |
| --- | --- | --- |
| 86–118 | 固定宽度，必现 | h2 418 / 400 / 408，h1 424 / 424 / 432 |
| 119–123 | 条件性，由 ECH GREASE 载荷决定 | ech=372 时 h2 补 28、h1 补 52；其余不补 |
| **124+** | **永不出现** | key_share 起 124 加入 X25519Kyber768 公钥（+1218 字节），CH 恒超 511，窗口不可达 |

最后一行是关键：它是一个硬性"必须缺席"，而不是可容忍的形态。

`tools/audit-padding-rule.py` 按上表逐连接判定 144 组 profile×协议证据。
双向变异测试：删掉 86 的 padding → FAIL；给 157 加 padding → FAIL。

实测分布（每版 24 次采样）与规则完全一致：

```
chrome_120 {(372,28)×5, 其余 null}
chrome_123 {(372,28)×8, 其余 null}
chrome_124 {(372,null)×5, 其余 null}   ← ech=372 也不再补
chrome_131 {(372,null)×7, 其余 null}
```

### 4.2 `http_version=v1` 时缺 17613（ALPS）是正确的，不是缺陷

用 `http_version=v1` 发请求、再拿 peetprint 去对 **h2** 抓包，152–157 会报两处 DIFF：

| 位置 | h2 抓包 | 我方 `v1` 请求 | 性质 |
| --- | --- | --- | --- |
| ALPN | `2-1.1` | `1.1` | `v1` 的设计就是强制单协议 |
| 扩展 | 含 17613 | 缺 17613 | ALPS 只对 `alpn_protos` 里的协议声明 |

根因在 `net/socket/ssl_client_socket_impl.cc`：循环遍历 `ssl_config_.alpn_protos`，
只有当**同一协议**在 `application_settings` 表里有条目时才
`SSL_add_application_settings`；而 `net/http/http_network_session.cc` 只给
`kProtoHTTP2` 装了条目。h1-only 时 `alpn_protos` 只剩 `http/1.1`，匹配不上，于是不发。

**这不是「我们与 Chrome 不一致」**：基准里 **h1 抓包本身就没有 ALPS**（86–157 全部版本，
6 次采样全无），而同一版本的 h2 抓包从 91 起有。所以我方 h1 输出与基准 h1 逐字段一致
（`extension_set` 与 `alpn` 均 PASS）。

**正确的对照方式**：`http_version` 与抓包必须是同一种协议——`v1` 对
`tls-results-h1.json`，native/H2 对 `tls-results.json`。

**不要被牵连的一项**：`trust_anchors`（0xca34）与协议无关，基准 h1 从 152 起也有，
我方 h1 同样发送。把它和 17613 一起当成"h1 本该没有"会引入真缺陷。

门禁：`tools/audit-h1-alps-absence.py` 断言 72 个 profile 的 h1 证据里
(a) 我方 h1 扩展集属于基准 h1 实际出现过的形态，(b) 不含任何 ALPS 扩展，
(c) 同版本 h2 基准确实带 ALPS（证明缺 17613 是协议属性而非 profile 丢了 ALPS）。
变异测试：向 chrome_157 的 h1 证据注入 `application_settings` → 门禁失败。

### 4.2 HTTP/1.1 默认请求头不由 profile 决定

基准的 H1 记录是浏览器导航头集（`sec-ch-ua*`、`Sec-Fetch-*`、`Accept-Encoding: zstd` 等 11–13 个），
本 Core 经 `URLRequest` 发出的是 API 客户端头集（4 个）。该边界在
`docs/COMPATIBILITY_BOUNDARY.md` 已声明，非本次引入。
HTTP/1.1 的 TLS 层指纹（cipher/扩展集/曲线/签名算法/ALPN）仍逐项断言并通过。

### 4.3 86–97 的 `supported_versions` 差异：已按 profile 复原（改 Chromium）

这一项此前被我标成"找不到编码差异"。找到了，而且不是编码问题：

| Chrome | `kDefaultSSLVersionMin` | supported_versions | CH 影响 |
| --- | --- | --- | --- |
| 86–97 | `SSL_PROTOCOL_VERSION_TLS1` (0x0301) | 1.3 / 1.2 / **1.1 / 1.0** | +4 字节 |
| 98–157 | `SSL_PROTOCOL_VERSION_TLS1_2` (0x0303) | 1.3 / 1.2 | 基准 |

依据（逐 tag 核对）：

- `net/ssl/ssl_config.cc:13` — 86/97 是 `TLS1`，98 起改成 `TLS1_2`
- `net/ssl/ssl_config_service.h` — 98 的注释同时改为 "TLS 1.0/1.1 are not supported"
- `ssl/ssl_versions.cc` — 两代 `ssl_add_supported_versions` 都按 `hs->min_version` 过滤，**编码逻辑无差异**

#### 改动

`net/socket/ssl_client_socket_impl.cc` 原本硬拒低于 TLS 1.2 的配置：

```cpp
if (version_min < TLS1_2_VERSION || version_max < TLS1_2_VERSION) {
  return ERR_UNEXPECTED;   // TLS versions before TLS 1.2 are no longer supported.
}
```

改为**按 profile 生效**，并保持默认路径不变：

- `SSLContextConfig` 新增 `profile_allows_legacy_tls_versions`（默认 `false`）
- `ProfileSSLConfigService` 仅当该 release 记录的 `tls_version_min < 0x0303` 时置真
- `ssl_config.h` 补回 `SSL_PROTOCOL_VERSION_TLS1` / `TLS1_1` 两个码点（`kDefaultSSLVersionMin` **仍是 TLS1_2**）
- 上限不得低于 TLS 1.2，profile 只能降下限、不能降上限

#### 验证

| 情形 | 结果 |
| --- | --- |
| chrome_86 / 97 | `supported_versions = GREASE,1.3,1.2,1.1,1.0`，握手成功（h2） |
| chrome_98 / 118 / 157 | `GREASE,1.3,1.2`，握手成功（h2） |
| 默认 profile（`chromium_current`） | 仍为 `GREASE,1.3,1.2` —— **没有 profile 时下限不变** |
| `minicronet_smoke` | 通过（含 `test_profile_gate` 与 `chrome_86` 下界断言） |

**证据是直接读出来的，不是推出来的。** `supported_versions` 的版本列表就在
`profiles/chrome_<major>/h2/captures.json` 的 `tls.extensions` 里，字段名
`supported_versions (43)`，载荷键 `versions`：

```
chrome_86  ['TLS_GREASE (0xaaaa)', 'TLS 1.3', 'TLS 1.2', 'TLS 1.1', 'TLS 1.0']
chrome_97  ['TLS_GREASE (0x0a0a)', 'TLS 1.3', 'TLS 1.2', 'TLS 1.1', 'TLS 1.0']
chrome_98  ['TLS_GREASE (0xcaca)', 'TLS 1.3', 'TLS 1.2']
chrome_157 ['TLS_GREASE (0x8a8a)', 'TLS 1.3', 'TLS 1.2']
```

`tools/audit-tls-version-range.py` 现在两侧都断言：表里 86–97 的下限是 `0x0301`、98+ 是
`0x0303`，并且**每一条连接的 `versions` 列表**（去掉 GREASE 后）必须等于该下限蕴含的
列表，GREASE 项必须恰好一个。变异测试：把 chrome_86 的 `versions` 里删掉 TLS 1.1/1.0，
门禁 exit 1 并报 `supported_versions is ['TLS 1.3', 'TLS 1.2'], expected [...]`。

（此前这一条只有间接支撑：对齐报告不记录该扩展的载荷，86–97 的结论只能从「padding 宽度
比 98–118 宽 10 字节，正好等于 TLS 1.0/1.1 两个版本字」反推。现在那层间接性没有了。）

安全属性：TLS 1.0/1.1 只在该 release 的 profile 被显式选中时进入 ClientHello，默认连接与既有行为完全一致。

#### 附带修掉的两处

- `smoke.c` 的 `test_profile_gate` 用 `chrome_98` 当"不存在的 profile"示例；98 现在是真 profile，改为 `chrome_85`（表范围外）
- `audit-h3-profile-support.py` 的布尔字段分类漏了两个新的 `uint16_t`，会让 H3 标志位错位

#### 门禁

新增 `tools/audit-tls-version-range.py`：读生成表断言 86–97 为 0x0301、98+ 为 0x0303，并交叉校验边界两版的 evidence 均为 PASS。变异测试：把 86 改成 0x0303 → FAIL。

### 4.3 User-Agent 平台令牌随宿主

版本号来自 profile，平台令牌按 `BUILDFLAG(IS_WIN/IS_MAC)` 与 `ARCH_CPU_*` 跟随本机，
因此 Linux 宿主发 `X11; Linux x86_64`，基准是 Windows。只断言 `Chrome/<version>` 令牌。

## 5. 顺带修复的既有缺陷

| 缺陷 | 位置 | 判定依据 |
| --- | --- | --- |
| `HistoricalUserAgent` 只识别 `major >= 99`，86–98 退回 `minicronet/<ver>` | `core/source/minicronet.cc` | 修复前 chrome_86 实测 UA = `minicronet/0.2.8` |
| 版本表只到 104，且 101/104 的 build 段与基准不符 | 同上 | 基准 101=`101.0.4951.67`、104=`104.0.5112.102` |
| **86–92 强制 H3 会真的发出 QUIC 并返回 200** | `core/source/minicronet.cc` | 修复前 chrome_86 FORCE_H3 实测 HTTP 200 |
| H3 `SETTINGS_H3_DATAGRAM`(51) 在 93–119 也被发送 | `net/quic/*`（并入 `profile-context-net.patch`） | 基准 93–119 无 51、120+ 有 |
| `h3_disabled` 语义被误当作「不支持 H3」 | profile schema 与生成器 | 99–122 全部被误标；实测每版都有 6 次可用 H3 抓包且对齐 PASS |
| 生成器 vs 提交表的 chrome_148 `try_quic_by_default` 不一致 | `profile_table_generated.h` | 用 HEAD 输入+HEAD 生成器重算得 `true`，提交值 `false` |
| `verify-wire-capture.py` 无条件要求 `ech_digest` 随机 | 同上 | 不发 ECH 的版本恒 1 个 unique 被判失败 |

## 6. 仍需确认的事项

1. RFC 7685 padding 已按上游规则复现（见 §4.2），不再是待确认项。仍是**条件性**结果：
   某一次连接是否出现 padding 取决于该次 ClientHello 落在哪个长度区间，Chrome 亦然。
   能保证的是机制（同样的长度进同样的分支），不是「每次都出现」。
2. 本机 Chromium 是 shallow clone，tag 级证据全部经由 googlesource 按 tag 拉取校验，不是本地 checkout。
3. 若要为 86–92 补上 H3，需要先取得这些版本可用的 QUIC 抓包（同端点、每版 ≥6 次成功）；
   在此之前它们保持「明确不支持」。
