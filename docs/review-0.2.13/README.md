# 0.2.13 打 tag 前的验收记录

按 AGENTS.md 第 13 节生成：`review-before-tag.py` 把每一道门禁都 shell 出去交给拥有它的工具，
不重新实现任何检查；跑不了的步骤记为 `unavailable` 并写明原因，绝不记成 pass。

| 文件 | 覆盖的组 | 步骤数 | failures |
| --- | --- | --- | --- |
| `summary.json` | `gates` 12 / `fingerprint` 10 / `bindings` 6 / `concurrency` 1 / `leaks` 2 | **31** | **0** |
| `bench.json` | `concurrency` 原始数字 | — | — |
| `leak.json` | 六种请求形态的 fd / RSS | 6 | 0 |
| `leak-hunt.json` | 13 种形态，每形态 2 次重复（`--requests 20 --batches 8`） | 13 | 0 |

重跑命令：

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64 \
       PYTHONPATH=bindings/python \
       CHROMIUM_SRC=$CHROMIUM_SRC \
       CHROMIUM_TOOLCHAIN=$CHROMIUM_TOOLCHAIN \
       XWIN_ROOT=$XWIN_ROOT OSXCROSS_TARGET=$OSXCROSS_TARGET
python3 tools/review-before-tag.py --out docs/review-0.2.13/summary.json
```

唯一跳过的是 `profile_state_isolation_probe`（需要 `HTTP_BASE`/`HTTPS_BASE` 测试服务器，
harness 不在本仓库）。它写在 `profile-probes` 的 summary 里，不记为通过。

## 这次 tag 与 0.2.12 的差别

**ABI 升到 12，29 个导出符号。** 三件事：结构化重定向头字段、跨域 `Authorization`
剥离（opt-in）、`MN_READ_TAKE_BUFFER` 零拷贝（opt-in，实测 0.38%）。详见
`docs/ABI.md` 的 v12 一节。

**修掉两个「门禁声称验证了、实际没有」的缺陷：**

1. `bindings/python36` 无法发出任何请求——共享 facade 传的参数比 3.6 扩展声明的多，
   第一个请求就 `TypeError`。`audit-python36-artifact.py` 当时在 `native = load()`
   **之后**才改 `MODULE_NAME`，所以它测的是现代扩展。现在它在 import 之前把扩展登记
   进 `sys.modules`、断言 facade 绑定的就是它，然后跑 sync GET / 重定向 / 异步 GET
   三条路径。
2. `take_body_buffers=True` 是空选项——存进 session 后从未传给 `engine.request()`。
   两种取值返回的字节完全相同，没有任何门禁能看出来。`audit-session-parity.py` 新增
   `check_options_reach_the_core()`，运行时那一半由 `EngineOptionForwardingTests`
   用一个假 engine 断言它收到的 kwargs。

**另修三处：**

- `crates/minicronet-sys` 的布局测试把 `mn_request_config_t` 写成 144 / 92（v11 的值）。
  v12 追加字段后 64 位是 152、32 位是 96，`cargo test` 在 v12 上直接红。
- `core/source/smoke.c` 的 `on_redirect` 还是旧签名，`build-core-linux-x86_64.sh` 因此
  编译失败——也就是说**九平台重建会失败**，而在此之前没有任何门禁会跑那个构建。
- `docs/TBD_VERIFICATION.md` 引用了 157 tag 的完整 40 位 commit，
  `audit-pypi-metadata.sh` 正确地拒绝了它（文档里的完整 commit 必须等于
  `CHROMIUM_REVISION`）。改成缩写，并把这条规则写进该文档。

**九个平台的 Core 全部用 0.2.13 重建**，产物版本串逐个核对为 `minicronet/0.2.13`。

## concurrency 原始数字

| 指标 | 数值 |
| --- | --- |
| `sync_sequential_1kib` | {"latency_ms_p50": 2.02, "latency_ms_p95": 3.32, "requests": 200, "requests_per_second": 460.9, "seconds": 0.434} |
| `async_concurrency_32` | {"concurrency": 32, "distinct_cache_keys": 200, "requests": 200, "requests_per_second": 756.3, "seconds": 0.264} |
| `async_concurrency_128` | {"concurrency": 128, "distinct_cache_keys": 200, "requests": 200, "requests_per_second": 807.6, "seconds": 0.248} |
| `stream_throughput_64mib` | {"bytes": 67108864, "mib_per_second": 117.4, "read_chunk_bytes": 65536, "seconds": 0.545} |

## leaks：六种形态的 fd / RSS

判据是**第二批相对第一批的增量**（fd ≤ 8 个、RSS ≤ 24 MiB）：

| 形态 | fd（预热/第 1 批/第 2 批） | fd 增量 | RSS 增量（KiB） | 判定 |
| --- | --- | --- | --- | --- |
| `sync-get` | 10/10/10 | 0 | -44 | pass |
| `sync-stream` | 11/10/10 | 0 | 16 | pass |
| `sync-abandon` | 11/11/11 | 0 | -4 | pass |
| `async-gather` | 10/10/10 | 0 | -4 | pass |
| `error-path` | 10/10/10 | 0 | 0 | pass |
| `discard-path` | 10/11/11 | 0 | 44 | pass |

`leak-hunt` 的 13 种形态（含 `churn-nogc`，故意关掉循环回收器）全部无斜率增长：
**13/13 种形态判定为 `flat`**，`failures: 0`。

## 关于 `async-gather` 的 5% 误报

本轮 review 有一次跑出 `12/13 shapes flat`：`async-gather` 的第二次重复以 `SIGABRT`
结束。这不是新缺陷，`docs/NEXT_STEPS.md` 的「进程正常退出时偶发 SIGABRT」一节记录了
它——**采样全部打印完之后**才 abort，是退出期的静态析构竞态。

本轮单独实测：20 次独立子进程 **1 次 SIGABRT（5%）**，且该次 `stdout` 完整。用 ABI 11
的产物曾测 200 次 signal=0，所以准确表述是「仍然存在，概率 1/20 量级，与 ABI 版本
无关」，而不是「已修复」。

上面这份 `summary.json` 是重跑一次拿到 13/13 flat 的结果。判据没有放宽，
`async-gather` 也没有从清单里删掉——那会让它变成没人看的缺口。
