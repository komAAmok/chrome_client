# 0.2.14 打 tag 前的验收记录

按 AGENTS.md 第 13 节生成：`review-before-tag.py` 把每一道门禁都 shell 出去交给拥有它的工具，
不重新实现任何检查；跑不了的步骤记为 `unavailable` 并写明原因，绝不记成 pass。

| 文件 | 覆盖的组 | 步骤数 | failures |
| --- | --- | --- | --- |
| `summary.json` | `gates` 12 / `fingerprint` 10 / `bindings` 6 / `concurrency` 1 / `leaks` 2 | **31** | **0** |
| `bench.json` | `concurrency` 原始数字 | — | — |
| `leak.json` | 六种请求形态的 fd / RSS | 6 | 0 |
| `leak-hunt.json` | 13 种形态，每形态 2 次重复 | 13 | 0 |

重跑命令：

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
       PYTHONPATH=bindings/python \
       CHROMIUM_SRC=$CHROMIUM_SRC \
       CHROMIUM_TOOLCHAIN=$CHROMIUM_TOOLCHAIN \
       XWIN_ROOT=$XWIN_ROOT OSXCROSS_TARGET=$OSXCROSS_TARGET
python3 tools/review-before-tag.py --out docs/review-0.2.14/summary.json
```

唯一跳过的是 `profile_state_isolation_probe`（需要 `HTTP_BASE`/`HTTPS_BASE` 测试服务器，
harness 不在本仓库）。它写在 `profile-probes` 的 summary 里，不记为通过。

## 这次 tag 与 0.2.13 的差别

两个「被接受但没生效」的 DNS 选项，都改成 fail-closed 抛错。

**`dns_over_tls` 不是「用 DoT 解析」，是「关掉内置解析器」。** Chromium 桌面版没有
DoT 客户端，`net/dns/` 里只有明文 `dns_tcp_attempt`，853 端口在解析器里一次都不出现。
`dns_over_tls_active` 是闸门不是传输选择：`DnsClient::CanUseInsecureDnsTransactions()`
在它置位时返回 false（`net/dns/dns_client.cc:168`），内置解析器拒绝每个 job，
`TaskType::SYSTEM` 接手，`dns_nameservers`/`doh_template` 全部被丢弃。实测：配置的
服务器收到 4 个查询（A + HTTPS RR）时为 0，收到 0 个（UDP 与 TCP 都是 0）时为 1，
请求两种情况下都返回 200。

**`dns_padding` 从 ABI 走到 `EngineOptions` 就断了。** `core/source/engine.cc` 里 grep
计数是 0——从没读过；Chromium 也没有任何 EDNS0 padding 控制。调用方要求填充查询，
实际发出未填充的，且没有任何提示。

四个 DNS 选项里两个是假的，而 README 当时把四个都标成 ✓。

**修法**：绑定层抛 `UnsupportedFeature`，Core 对非 0 值返回 `MN_ERROR_INVALID_ARGUMENT`
（第二道防线），ABI 头两个字段改成「MUST be 0」并写清实测数字与源码行。

**新增门禁**：`audit-session-parity.py` 的 `check_refused_options_are_refused()`
——登记在 `REFUSED_NOT_STORED` 的选项一旦出现 `self.<name> = bool(...)` 这种
「存起来当真选项」的形状就红。变异测试：把 `self.dns_padding = False` 改回
`bool(dns_padding)` → 门禁报出完整原因；恢复后绿。

**九个平台的 Core 全部用 0.2.14 重建**，产物版本串逐个核对为 `minicronet/0.2.14`。

## concurrency 原始数字

| 指标 | 数值 |
| --- | --- |
| `sync_sequential_1kib` | {"latency_ms_p50": 1.54, "latency_ms_p95": 2.48, "requests": 200, "requests_per_second": 599.0, "seconds": 0.334} |
| `async_concurrency_32` | {"concurrency": 32, "distinct_cache_keys": 200, "requests": 200, "requests_per_second": 1022.2, "seconds": 0.196} |
| `async_concurrency_128` | {"concurrency": 128, "distinct_cache_keys": 200, "requests": 200, "requests_per_second": 984.6, "seconds": 0.203} |
| `stream_throughput_64mib` | {"bytes": 67108864, "mib_per_second": 147.7, "read_chunk_bytes": 65536, "seconds": 0.433} |

## leaks：六种形态的 fd / RSS

判据是**第二批相对第一批的增量**（fd ≤ 8 个、RSS ≤ 24 MiB）：

| 形态 | fd（预热/第 1 批/第 2 批） | fd 增量 | RSS 增量（KiB） | 判定 |
| --- | --- | --- | --- | --- |
| `sync-get` | 10/10/10 | 0 | -44 | pass |
| `sync-stream` | 10/10/10 | 0 | -44 | pass |
| `sync-abandon` | 10/10/11 | 1 | 360 | pass |
| `async-gather` | 10/10/10 | 0 | 32 | pass |
| `error-path` | 10/10/10 | 0 | 0 | pass |
| `discard-path` | 10/11/11 | 0 | -56 | pass |

`leak-hunt` 的 13 种形态（含 `churn-nogc`，故意关掉循环回收器）全部无斜率增长：
**13/13 种形态判定为 `flat`**，`failures: 0`。

## 关于 `async-gather` 的偶发 SIGABRT

`docs/NEXT_STEPS.md` 记录的退出期静态析构竞态仍然存在（0.2.13 时实测 1/20）。
本轮 `leak-hunt` 13/13 flat，未复现。判据没有放宽，`async-gather` 也没有从清单里删掉。
