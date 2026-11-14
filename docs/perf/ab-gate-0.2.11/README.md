# 并发 A/B：两处 P0 修复的实测记录

本文回答一个具体问题：**同步与异步的并发能力有没有因为这一轮修复而变化**。
每条数字都由 `docs/perf/ab-gate-0.2.11/raw*/` 下的原始 JSON 经
`tools/perf-ab-summary.py` 重算得出，不是手抄。

实测环境与 `docs/PERF_TEST_AND_LEAK.md` 相同：linux-x86_64、8 核、
Core `core/binaries/linux-x86_64/libminicronet.so`（0.2.11，ABI v11）、Python 3.12.3。

---

## 0. 结论先行

| 路径 | 变化 | 是否可信 |
| --- | --- | --- |
| **mTLS 并发** | 吞吐 **x5.54**（135.7 -> 797.9 req/s）；TCP 连接 369 -> 5 | **是**：3/3 轮同向，连接数是确定性计数，量级差 5 倍以上 |
| 异步并发（不同 URL） | 两序列共 10 次配对，落在 -3.92% ~ +1.46% | **否**：换序后符号翻转，见 §3 |
| 异步并发（同一 URL） | 两序列共 10 次配对，落在 -3.28% ~ +1.21% | **否**：同上 |
| 同步并发 | 两序列共 10 次配对，落在 -2.48% ~ +0.59% | **否**：同上 |

一句话：**这一轮修复没有提高普适并发吞吐；它提高的是 mTLS 路径的并发能力，并修掉了两条并发正确性缺陷**。

---

## 1. 被测的两个版本

A/B 的两个臂只差两个文件（用 `git stash push -- engine.py sessions.py` 切换）：

| 臂 | 内容 |
| --- | --- |
| `original` | 修复前：`cert=` 每请求重注册；`discard_cookies` 清共享 store |
| `patched` | 当前树：`EngineSlot._applied_certs`；`discard_cookies` 走一次性引擎 |

Core 产物、Rust 绑定、Python 解释器、fixture 全部不变，所以差异只能来自这两处。

---

## 2. mTLS 并发：唯一有量级差异的路径

fixture 是真实 TLS 监听器且 `CERT_REQUIRED`——错的证书会握手失败，不会静默复用连接。
每条手臂先发 1 个请求预热，清零计数器，再发 64 个并发请求。

```bash
tools/mtls-concurrency.py --requests 64 --certs /tmp/mtls
```

三轮交错，每轮新进程 + 新 fixture：

| 轮 | original qps | original TCP | patched qps | patched TCP |
| --- | --- | --- | --- | --- |
| 1 | 135.7 | 369 | 865.8 | 5 |
| 2 | 132.9 | 369 | 736.7 | 5 |
| 3 | 204 | 162 | 797.9 | 5 |
| **中位** | **135.7** | **369** | **797.9** | **5** |

配对比值中位：**5.543x**（+454.3%）。

**为什么这不是噪声**：

1. 连接数不是计时量，是服务端 `accept()` 的计数。369 对 5 是确定性差异。
2. 原因可指认：`SSLClientContext::SetClientCertificate` 同步调
   `NotifySSLConfigForServersChanged`（`net/socket/ssl_client_socket.cc:274-287`），
   后者让 `HttpStreamPool` 刷新该 origin 的连接组并关掉空闲连接。原版每请求调一次，
   于是 64 个并发请求没有一个能复用连接。
3. 三轮里 original 的 qps 有波动（132.9–204.0），但**最快的那一轮**也远低于 patched
   最慢的一轮（736.7）。

16 并发时的单次对照：original 112.9 qps / 81 TCP，patched 247.4 qps / 5 TCP。

---

## 3. 普适并发：两次序列互相证伪，结论是「没有变化」

每个臂跑 5 轮。序列 1 固定「patched 先、original 后」，序列 2 把顺序对调。
若某个臂的差异是真的，换序不应改变它。

```bash
tools/concurrency-ab.py --requests 256,1024 --threads 8,32 \
    --out docs/perf/ab-gate-0.2.11/raw/conc-r$i-{patched,original}.json
tools/perf-ab-summary.py docs/perf/ab-gate-0.2.11/raw
```

| 臂 | 序列 1（patched 先） | 序列 2（original 先） | 判定 |
| --- | --- | --- | --- |
| `async 1024 one-URL` | -3.28% (5/5) | +0.4% (4/5) | **符号翻转 -> 噪声** |
| `async 1024 distinct` | +0.88% (4/5) | -1.82% (3/5) | **符号翻转 -> 噪声** |
| `async 256 one-URL` | -0.13% (3/5) | +1.21% (3/5) | **符号翻转 -> 噪声** |
| `async 256 distinct` | +1.46% (3/5) | -3.92% (3/5) | **符号翻转 -> 噪声** |
| `sync 32` | -2.48% (5/5) | +0.59% (3/5) | **符号翻转 -> 噪声** |
| `sync 8` | -1.36% (3/5) | -1.32% (3/5) | 未翻转，但幅度在离散度内 |

> **机器可读产物现在与本文一致。** 此前 `summary.json` 里 `conclusive: true` 出现在
> 上面两个"5/5 一致"的臂上，而本文用散文否掉了它们——只读 JSON 的人会得到相反结论。
> `tools/perf-ab-summary.py` 已把该字段改名为 `sign_test_conclusive`（它只描述**单一
> 序列**的符号检验），并新增 `--swapped <dir>` 输出 `reversed_and_agrees`。
> 用仓库里的两份数据重算（`raw` 与 `raw-swapped`），**六条配对全部为 `false`**，
> 即：没有任何一条在换序后仍然成立。重算命令：
>
> ```bash
> tools/perf-ab-summary.py docs/perf/ab-gate-0.2.11/raw \
>     --swapped docs/perf/ab-gate-0.2.11/raw-swapped
> ```

### 3.1 一个被自己实验推翻的结论

序列 1 里有两个臂是 **5/5 一致**：`async one-URL @1024` 为 -3.28%，`sync 32 线程` 为 -2.48%。
按常用的「符号检验 n=5 需 5/5」判据，它们看起来是真实退化。

没有按那个判据下结论，而是去查了测量流程本身：**每一轮里 patched 总是先跑**。
后跑的臂面对的是刚被前一个臂跑过 1024 并发、socket 池与 `TIME_WAIT` 都还没回落的机器。
换序之后：

- `async one-URL @1024`：-3.28% -> **+0.4%**
- `sync 32 线程`：-2.48% -> **+0.59%**

两个 5/5 的「结论」同时反号，说明它们量的是顺序，不是版本。
**规则**：配对实验的臂顺序必须每轮交换，或至少做一次换序复测；
否则「5/5 一致」这种看起来很硬的判据，量到的可能只是固定偏差。

### 3.2 噪声下限

| 臂 | 臂内离散度（original, req/s） | 两臂中位数之差 |
| --- | --- | --- |
| `async 1024 one-URL` | 8.7 | 2.4 |
| `async 1024 distinct` | 86.6 | 8.4 |
| `async 256 one-URL` | 22.4 | 1.0 |
| `async 256 distinct` | 61.7 | 47.2 |
| `sync 32` | 91.3 | 16.9 |
| `sync 8` | 59.8 | 9.0 |

每一行里臂内离散度都**大于**两臂中位数之差。这是「测不出来」的直接证据，
不是「差不多相等」的委婉说法。

---

## 4. 为什么普适并发不会变（可指认的原因）

不是「优化没生效」，而是这三处改动都不在那个热路径上：

| 改动 | 触及的路径 | 为什么吞吐出不来 |
| --- | --- | --- |
| `EngineSlot._applied_certs`（P0-1） | 仅 `cert is not None` 时执行 | 非 mTLS 请求根本不进这个分支 |
| 一次性引擎（P0-2） | 仅 `discard=True` 时执行 | 本组测试不含该选项；且它**多建一个上下文**，是变慢不是变快 |
| `EngineCache` O(1) 命中（P2-1） | 每请求一次 | 10.9 us vs 网络线程每请求 530–700 us，占 1.5%，落在噪声里 |

`MiniCronetNet` 的每请求 CPU（5 轮均值，两臂之差）：

| 臂 | 差值（ms/请求） | 相对 0.53–700 µs 的量级 |
| --- | --- | --- |
| `async 256 distinct` | -0.031 | 可忽略 |
| `async 256 one-URL` | -0.008 | 可忽略 |
| `async 1024 distinct` | -0.004 | 可忽略 |
| `async 1024 one-URL` | **+0.486** | 这是 HttpCache 排队区间的抖动，不是代码差异：同臂 5 轮本身就在 12.23–13.69 之间 |
| `sync 8` | -0.012 | 可忽略 |
| `sync 32` | +0.014 | 可忽略 |

这张表要说明的是：**改动没有落在这些路径的 CPU 上**。最后一行那个 0.486 恰恰是
该臂噪声的另一种量法，不能当作「patch 更贵」的证据——它的臂内跨度比两臂差大一个量级。

---

## 5. 这一轮真正的收益

不在吞吐，在**并发正确性与资源**：

| 项 | 修复前 | 修复后 | 证据 |
| --- | --- | --- | --- |
| `discard_cookies` 并发串味 | 20 次读里 **19 次错** | **0 次错** | `test_session_defects.DiscardCookiesIsolationTests`；变异验证：改回 `clear_cookies()` 即变红 |
| mTLS 连接数 | 369 / 64 请求 | **5** | 本文 §2 |
| mTLS 单请求 p50 | 46.77 ms | **1.75 ms** | 同一 fixture，见审计报告 §1.2 |

---

## 6. 复现

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64
export PYTHONPATH=bindings/python
ulimit -n 60000

# 并发矩阵，一个臂一次
tools/concurrency-ab.py --requests 256,1024 --threads 8,32 --out /tmp/arm.json

# mTLS 并发（需要一套 CA + 服务端证书 + 客户端证书，见 test_client_certificates.py）
tools/mtls-concurrency.py --requests 64 --certs /tmp/mtls

# 两个臂之间切换
git stash push -- bindings/python/chrome_client/_python_impl/engine.py \
                   bindings/python/chrome_client/_python_impl/sessions.py

# 从原始 JSON 重算汇总
tools/perf-ab-summary.py docs/perf/ab-gate-0.2.11/raw \
    --out docs/perf/ab-gate-0.2.11/summary.json
```

---

## 7. 本机不可用的测量（如实声明）

| 手段 | 状态 | 原因 |
| --- | --- | --- |
| `perf record` | 不可用 | `perf_event_paranoid = 4` |
| 火焰图 | 不可用 | 依赖 perf |
| ASan 版 Core | 未做 | 需要重编 8 个平台产物，不在本轮范围 |

替代：读 `MiniCronetNet` 线程的 `/proc/self/task/<tid>/stat` utime+stime 差分。
它回答「网络线程花了多少 CPU」，不回答「花在哪一行」——后者需要 perf。

---

## 8. 文件索引

| 文件 | 内容 |
| --- | --- |
| `docs/perf/ab-gate-0.2.11/raw/` | 序列 1：5 轮 x 2 臂并发 + 3 轮 x 2 臂 mTLS |
| `docs/perf/ab-gate-0.2.11/raw-swapped/` | 序列 2：换序复测，10 个并发 JSON |
| `docs/perf/ab-gate-0.2.11/summary.json` | 序列 1 汇总（中位数、配对比值、符号检验） |
| `docs/perf/ab-gate-0.2.11/summary-swapped.json` | 序列 2 汇总 |
| `tools/concurrency-ab.py` | 并发矩阵探针，输出 JSON |
| `tools/mtls-concurrency.py` | mTLS 并发探针，输出连接数与握手数 |
| `tools/perf-ab-summary.py` | 从原始 JSON 重算汇总 |
