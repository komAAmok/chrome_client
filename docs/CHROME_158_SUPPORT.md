# chrome_158 验收记录

记录时间：2026-11-23。所有数字都可由本文命令复现，没有一栏是推断出来的。

本文只记录 chrome_158 这一个版本的验收；86–157 的验收记录见
[CHROME_86_157_SUPPORT_MATRIX.md](CHROME_86_157_SUPPORT_MATRIX.md)，那份写于
2026-11-07，覆盖的是当时已存在的 72 个 profile，未回溯修改。

## 0. 结论

| 协议 | 结论 |
| --- | --- |
| HTTP/1.1 | 6 条完整连接，稳定字段六条全等 |
| HTTP/2（native） | 6 条完整连接，稳定字段六条全等 |
| HTTP/3 | 6 条完整连接，稳定字段六条全等 |

三级证据链 `--levels 1,2,3` 全部 **PASS**。

## 1. 抓包来源与一处必须写明的不一致

抓包采集自 **`158.0.8127.37`**，三协议各 6 条独立连接，6 个不同 session。

AGENTS §9 要求源码基准取该大版本**最新 tag**，因此源码侧取
**`158.0.8127.99`**（commit `a67074ee`）。两个 tag 不同，这一点必须说清楚：

| 项 | `.37`（抓包） | `.99`（源码基准） | 是否影响线缆 |
| --- | --- | --- | --- |
| `boringssl_revision` | `ea9f3a34` | `ea9f3a34` | 相同 |
| `quiche_revision` | `132bfd90` | `132bfd90` | 相同 |
| 九份审计源码 | — | — | 八份逐字节相同；`base/rand_util.cc` 变化 |

`base/rand_util.cc` 承载的是 QUIC 随机源信号（`quic_random`），不是线缆可观测字段，
因此 `.99` 的审计结论适用于 `.37` 的抓包。判据不是推断：两个 tag 的 BoringSSL 与
QUICHE revision 完全相同，而线缆指纹由这两个库与 `net/` 的协议代码决定。

## 2. 第 1 级：源码证据

```sh
python3 tools/audit-profile-addition.py --levels 1 --profile chrome_158
```

| 项 | 值 |
| --- | --- |
| 该大版本最新 tag | `158.0.8127.99` |
| profile 记录的 tag | `158.0.8127.99` |
| `recorded_tag_is_latest` | `true` |
| `boringssl_revision`（来自该 tag 的 DEPS） | `ea9f3a34c3977144c45d272be81842fd891a0bdb` |
| `quiche_revision`（来自该 tag 的 DEPS） | `132bfd90ea370faeacee616c70d10210bb12ba66` |
| 第 1 级实审源码 | 9 份（alpn / boringssl_extensions / boringssl_handshake_client / features / http2 / http3 / quiche_h3 / tls / websocket），sha256 与记录一致 |
| `source-evidence.json` 记录 | 12 份（另含 boringssl_random、chromium_base_random、quiche_random 三份随机源文件） |

## 3. 第 2 级：抓包

```sh
python3 tools/audit-profile-addition.py --levels 2 --profile chrome_158
```

| 协议 | 连接数 | 稳定字段 | 随机字段 |
| --- | --- | --- | --- |
| h1 | 6 | ciphers, extensions, http2_settings, supported_versions, user_agent_version | client_random, grease_values, session_id |
| h2 | 6 | 同上 | 同上 |
| h3 | 6 | extensions, h3_hash, h3_settings, h3_text, ja4, quic_version, selected_version, transport_parameters | client_random, destination_connection_id, source_connection_id |

trust anchor：h1 与 h2 的 wire 侧 28 个 ID 与表侧 28 个逐一匹配；h3 抓包的
`trust_anchors` 扩展在（共 12 个扩展），但**不带 `data` 字段**，没有可提取的 ID
列表，因此该字段记 `null` 而不是伪造匹配。

## 4. 第 3 级：产物

```sh
python3 tools/audit-profile-addition.py --levels 3 --profile chrome_158
```

用 `core/binaries/linux-x86_64/libminicronet.so` 发 6 次真实请求，
`live_connections: 6`。

**限定**：第 3 级只在 linux-x86_64 上执行，因为只有它能在构建机上加载运行。其余八个
产物通过静态验证（内嵌 profile 表、ABI 符号表、版本串、载荷逐字节、源码指纹、构建参数
快照），跨平台运行需要各自的原生机器。

## 5. 与 chrome_157 的差异

**线缆可见的稳定字段全部不变**：

- de-GREASEd cipher 列表、扩展集合、`supported_versions` 相同；
- HTTP/2 指纹相同：`1:65536;2:0;4:6291456;6:262144|15663105|0|m,a,s,p`；
- 28 个 trust-anchor ID 相同。

差异只有 UA 主版本串与 BoringSSL/QUICHE revision，后者是上游在两个版本之间滚动的。

## 6. 落盘内容

```
profiles/chrome_158/
├── meta.json                  version 158.0.8127.37，三协议各 6/6
├── README.md
├── source-evidence.json       12 份源码的 sha256 与两个 revision（其中 9 份被第 1 级实审）
├── h1/captures.json           6 条原始响应对象（JSONL，一行一条连接）
├── h1/samples.json            6 条 checks（ja3/ja4/peetprint/session/random）
├── h2/captures.json  h2/samples.json  h2/validation.json
└── h3/captures.json  h3/samples.json
```

`captures.json` 是端点返回对象的原样拼接，一行一条连接；`samples.json` 补上响应里
没有的 ja3/ja4/peetprint 哈希、session id、client random 与 header count。
