# 并发压测、性能归因与内存泄漏排查

本文是这个仓库的**性能工程手册**，不是一次性的报告。每个数字都能用本文给出的命令
重跑；每个结论都附「如何验证」。实测环境：linux-x86_64、8 核、8 GiB、
Core 产物 `core/binaries/linux-x86_64/libminicronet.so`（0.2.11，ABI v9）。

工具全部在 `tools/` 下，共享一个 fixture 与一套计数器：

| 文件 | 作用 |
| --- | --- |
| `tools/perf_harness.py` | 线路级故障注入服务器 + RSS/fd/线程/CPU 采样器 + 统计 + 两种驱动 |
| `tools/load-matrix.py` | 并发梯度 × 请求形态 × 故障注入的矩阵，输出 JSON |
| `tools/prof-attr.py` | 分层归因：线程状态/wchan、syscall、原生栈、Python 热点、perf 可用性 |
| `tools/leak-hunt.py` | 逐形态的增长斜率（含后半程斜率）、fd 种类归因、最小复现生成器 |
| `bindings/python/tests/test_resource_release.py` | 泄漏缺陷的回归测试（含变异验证） |
| `bindings/python/tests/test_client_alias.py` | `Client`/`AsyncClient` 别名与本次修复的行为回归（含变异验证） |
| `bindings/python/tests/test_max_clients_gate.py` | `max_clients` 门边界（两种模式）的实测门禁（含变异验证） |

`tools/leak-probe.py`（发布门禁用）与 `tools/bench-core-baseline.py`（基线）保持原样：
它们回答「平不平」和「快不快」，本文的工具回答「为什么」和「哪一条路径」。

---

## 0. 库的基本信息（按本仓库实测填写，不是假设）

原问题里留空的五项，按代码与产物填：

| 项 | 实际值 | 证据位置 |
| --- | --- | --- |
| 语言/运行时 | Chromium C++ Core（`libminicronet`）+ Rust 安全层 + PyO3 abi3-py39 绑定 + Python facade。无独立执行器 | `core/source/engine.cc`、`crates/minicronet/src/lib.rs`、`bindings/python/Cargo.toml` |
| 同步实现方式 | **调用线程阻塞**在 Rust `Condvar` 上，等 Core 回调线程投递事件；阻塞期间通过 `py.detach` 释放 GIL。不是「每请求一线程」，也不是协程池 | `crates/minicronet/src/request.rs:193-196`、`bindings/python/src/lib.rs` 的 `py.detach` |
| 异步实现方式 | asyncio `Future` + `loop.call_soon_threadsafe`；`max_clients` 落到 `asyncio.Semaphore`；**没有**自带事件循环，底层与同步共用同一个网络线程 | `sessions.py` 的 `AsyncSession`、`_AsyncState` |
| 底层 IO | Chromium `MessagePumpLibevent`（epoll）+ 全局 `ThreadPoolInstance` + 单条 `network_thread_`（`MiniCronetNet`） | `core/source/engine.cc:253-276` |
| 连接管理 | **每个 Engine 一整个 `URLRequestContext`**：独立 socket 池、H2/H3 池、TLS session cache、DNS cache、内存 HttpCache。Session 默认 `max_engines=8` 且 LRU 淘汰。keep-alive 由 Chromium socket 池自己维持，facade 的 `upkeep()` 是 no-op | `engine.py:DEFAULT_MAX_ENGINES`、`sessions.py` 的 `upkeep` |
| 依赖 | Chromium `58bcc148…`（156.0.8078.31）、BoringSSL、QUICHE、PyO3 0.30.1 | `CHROMIUM_REVISION`、`docs/PROJECT_STATUS.md` |

这一节决定了后面的每一个测试选择。两条最重要的推论：

1. **吞吐上限不由「客户端并发数」决定，而由单个网络线程 + Engine 数量决定。**
   同一 Engine 上 N 个并发请求全部排队在一条 IO 线程上；要更多并行度只能开更多
   Engine（多线程共享 Session 时靠不同的 per-request 配置命中不同 slot）。
2. **同步与异步不是两套实现**，只是同一栈上的两种等待方式。因此两者的数字必须能
   互相解释；如果异步突然慢 20 倍，那是驱动方式的问题，不是「异步实现慢」。

---

## 1. 测试矩阵

矩阵是**梯度**，不是清单：单点跑「高并发」无法区分「客户端饱和」和「fixture 饱和」。

### 1.1 请求形态（`tools/load-matrix.py --shapes`）

| 形态 | URL | 它测的是什么 |
| --- | --- | --- |
| `small-1k` | `/small?size=1024` | 每请求固定开销：引擎查表、头解析、响应包装 |
| `body-64k` | `/small?size=65536` | 单次 socket 读；隔离 body 拷贝成本 |
| `chunked-4m` | `/chunked?chunks=64&size=65536` | 分块重组 + Rust body 队列背压 |
| `delay-10ms` | `/delay?ms=10` | 延迟主导：低并发测 fixture 定时器，高并发测「能否同时挂住 N 个事务」 |
| `status-500` | `/status?code=500` | 错误构造路径（无 body） |

流式（`stream=True` + `iter_content`）单独在 §4 的 H6 里测，因为它改变的是消费者
行为而不是服务端行为。

### 1.2 并发梯度

`--ladder 1,8,32,64`（`1,4,16,32,64,128` 是完整梯度）。每格请求数默认
`max(64, 8*concurrency)`，保证高并发格有足够样本算 p99（p99 需要至少 100 个样本才
不是插值出来的）。

同步用 `ThreadPoolExecutor`，异步用 `AsyncSession(max_clients=N)`——**驱动库自己的
限流器**，而不是在 harness 侧再套一层，否则测的是 harness。

### 1.3 payload / 超时 / 错误注入

| 维度 | 取值 | 为什么 |
| --- | --- | --- |
| payload | 1 KiB / 64 KiB / 4 MiB（分块与不分块各一） | 跨越 64 KiB 会跨过 socket 读边界 |
| 超时 | 无 / 10 s（正常形态）；0.05 s（故障形态） | 0.05 s 用来把「超时路径」变成主路径 |
| 故障 | `stall`（收下请求不回）、`reset`（SO_LINGER 0 发 RST）、`truncate`（Content-Length 撒谎）、`refused`（拒绝端口）、`status-503`、`redirect-loop`（50 跳） | 每种都要在**线路上**发生，见 §2 |

### 1.4 一条必须遵守的纪律

**每个并发请求用不同 URL**（`&n=i` 去重）。Chromium 的 HttpCache 会把同 key 的事务
串行化，所以打同一个 URL 测到的是「缓存串行化」而不是并发度。
`--single-url` 保留了这个串行化形态，专门用来对照。

---

## 2. 工具选型：为什么不是 wrk / vegeta / JMeter

**被测对象是客户端。** wrk、vegeta、JMeter、ab、siege 都是**服务端**压测器：它们
产生负载打向一个服务器。这里要压的是客户端本身，外部压测器无法驱动它——最多只能
给「服务端」测出一个上限，然后这个上限会和客户端上限混在一起，无法归因。

所以选**自研 in-process harness + 独立 fixture**，理由有三条：

1. **故障必须发生在协议层。** `BaseHTTPRequestHandler` 表达不了「Content-Length 写
   两倍但只发一半」或「用 RST 而不是 FIN 关闭」。在 Python 对象层模拟的故障，测出来
   的是客户端对着一个谎话的反应，不是它对真实网络的反应。因此 fixture 是手写的
   socket 服务器（`perf_harness._Handler`），逐字节写响应。
2. **必须有对照组。** 本机 `http.client` 直接连同一个 fixture，得到「不含被测库的
   天花板」。没有这个数，任何平台期都不能归因（`docs/LESSONS.md` 第 19 条：公网服务
   限流曾被误判为并发缺陷）。实测对照（1 KiB，`/small?size=1024`）：

   | 并发 | http.client 对照组 | 本库 sync | 本库 async |
   | --- | --- | --- | --- |
   | 1 | 2733 req/s | 351 | 303 |
   | 8 | 1020 req/s | 585 | 702 |
   | 32 | 1208 req/s | 322 | 635 |
   | 64 | 1090 req/s | 449 | 640 |

   对照组始终在 1000–2700 req/s，说明 **fixture 不是瓶颈**；本库的 300–700 req/s
   是客户端栈的真实成本，不是被 fixture 压住的结果。
3. **必须能在进程内采样。** RSS、fd 种类、线程数、CPU 都在被测进程里读，外部压测器
   看不到这些。

**什么时候仍然用外部工具**：验证服务端契约、或者验证「客户端在真实网络路径上的
行为」。那时用 `vegeta`/`wrk` 打目标服务，用本库做客户端，两者分开报告。

---

## 3. 指标与采集方法

全部由 `perf_harness.Sampler` 在**后台线程按 50 ms 采样**，而不是跑前跑后各读一次：
一个「峰值时才泄漏、结束时已经关掉」的描述符问题，端点测量看不见。

| 指标 | 采集方式 | 判读要点 |
| --- | --- | --- |
| QPS | `requests / wall` | 必须与对照组同格比较，否则是绝对数无意义 |
| 延迟 p50/p90/p99/p999 | 最近秩百分位（不插值） | 100 个样本的 p99 就是第 99 个真实样本 |
| **queue_ms（排队）与 service_ms（服务）** | 同步：提交时刻→拿到 worker；异步：`Semaphore` 准入前后各打一次点 | 见下方「一个真实的测量陷阱」 |
| CPU | `/proc/PID/stat` 的 utime+stime，换算成「用了几个核」 | 单网络线程的栈通常停在 1–2 核，见 §4 H3 |
| RSS | `/proc/PID/status` 的 `VmRSS`，取**峰值** | 只看末值会把中间峰值漏掉 |
| fd 总数 | `/proc/PID/fd` 计数 | 与「种类」一起看 |
| **fd 种类** | `readlink` 每个 fd，归类为 socket / dev-shm / memfd / anon-inode / eventpoll / eventfd | socket 增长=连接池；dev-shm/memfd=共享缓冲；anon-inode=定时器/eventfd |
| 线程数 | `/proc/PID/status` 的 `Threads` | 每请求新建线程会线性增长 |
| 每线程状态与 wchan | `/proc/PID/task/TID/{stat,wchan}` | `futex` 平 CPU = 停锁；`ep_poll` = IO 线程空转 |
| GC | Python：`gc.get_count()`/`gc.get_stats()`；分配用 `tracemalloc` | 本项目无 GC 压力（见 §4 H8），但**GC 是泄漏的掩盖者**，见 §5 |
| goroutine | 不适用（非 Go） | — |

### 一个真实的测量陷阱（已修，写进工具里）

第一版异步驱动在 `asyncio.gather` 里从**协程启动**开始计时，于是 gate=1、48 个请求
时报告 p50=98.8 ms，而同一请求的真实服务时间是 2.3 ms——测到的是信号量排队时间。
修复方式是把计时点移到**准入之后**，并把排队时间单独报告：

```
small-1k async c=1  qps=303  p50=2.797  p99=7.501  queue_p50=119.067
```

验证：`tools/prof-attr.py pyhot` 里 `session.get` 的 tottime 与 service p50 应当一致；
若不一致，说明计时点还在队列里。

---

## 4. 瓶颈假设清单与验证手段（含实测结论）

每条假设都给出**验证命令**和**本次实测结果**。结果为「不成立」的同样有价值：它把
优化预算从错误的方向上拿回来。

### H1 客户端栈的每请求固定开销在 facade（Python 层）

- 验证：`tools/prof-attr.py pyhot --concurrency 8 --requests 160`
- 实测：160 请求 / 2.196 s（含 fixture 线程），facade 侧最热的是
  `isinstance`(14471 次)、`dict.get`(12009 次)、`urlsplit`(480 次)、
  `_prepare_call`(0.114 s cumtime)。**单请求约 2.4 ms，其中 Python 侧可见部分
  远小于 1 ms**，其余时间在 `start_stream`/`await_response` 的 C 调用里。
- 原生栈（`tools/prof-attr.py stacks`，gdb 全线程 bt，sync c=4）把它钉死：

  ```
  6  __libc_recv
  5  futex_wait
  5  __GI___lll_lock_wait
  5  PyEval_RestoreThread
  4  ___pthread_mutex_lock
  3  <pyo3::marker::Python>::detach::<…PyResponse>
  3  <chrome_client_native::PyRequest>::__pymethod_start_stream__
  2  epoll_wait
  ```

  `start_stream` 的 `py.detach` 之后就是 `__libc_recv`：**每个请求的绝大部分墙钟
  时间在原生 recv 上**，Python 帧只在 detach/attach 的边界上出现。
- 结论：**不成立**（facade 不是主瓶颈）。继续优化 Python 层的收益上限是几十微秒。
- 但发现一个可测的子项：`trust_env` 每请求调用 `get_netrc_auth`，其中
  `os.path.expanduser` 每次触发 `getpwuid`（0.055 ms/请求）。实测
  `trust_env=True` 2.01 ms vs `False` 1.97 ms——**占 2%，不值得改**，
  但记在这里，因为它在容器里（无 passwd 条目）会变成 syscall 风暴。

### H2 单网络线程（`MiniCronetNet`）是吞吐天花板

- 验证：`tools/prof-attr.py threads --concurrency 8 --requests 120`，看
  `MiniCronetNet` 的 state/wchan 与 CPU ticks 增量；再看矩阵里 CPU 核数
- 实测：`MiniCronetNet` 全程 `ep_poll`，CPU ticks 增量 13/100 个 tick；整进程
  峰值 1.9 核。**网络线程没有饱和**。
- 结论：**部分成立**。饱和点不在 IO 线程，而在「每请求的 C++/Rust 往返 + Python
  等待唤醒」这条链上（见 H3、H4）。

### H3 锁竞争（Rust `Condvar`/`Mutex`）

- 验证：`threads` 子命令里 Python 线程若长时间 `futex_do_wait` 且 CPU 平，即为
  停锁；再用 `strace -f -c -e trace=futex` 看 futex 次数与 `read` 次数的比例
- 实测（`threads` 子命令，sync c=8）：Python 工作线程 24 条全部停在
  `futex_do_wait`，`MiniCronetNet` 停在 `ep_poll`，CPU ticks 增量个位数。
- 实测（`syscalls` 子命令，`strace -f -c`，sync c=8 × 64 请求，全进程合计）：

  | syscall | 次数 | 占 syscall 时间 |
  | --- | --- | --- |
  | `futex` | 6812（1302 次 EAGAIN 重试） | 68.4% |
  | `recvfrom` | 685（610 次 EAGAIN） | 21.7% |
  | `poll` | 8 | 5.6% |
  | `epoll_wait` | 509 | 1.6% |
  | `read` / `sendto` | 700 / 132 | 0.5% / 0.2% |

- 判读：`futex` 次数 ≈ 10× `epoll_wait`，但**这不是锁争用**。判据是「谁在等、
  谁在跑」：等待方（Python 工作线程）在 `futex`，被等待方（`MiniCronetNet`）在
  `ep_poll` 且 CPU 低。争用的形态相反——等待方和被等待方都在 `futex`，且 CPU 打满。
- 结论：**不成立**（是条件变量等待，不是互斥锁争用）。真正的成本是**每次事件唤醒
  都要跨 Rust→Python 一次**：`futex` 占 68% 的 syscall 时间，与 §8 结论 1
  「每请求往返开销」一致。

### H4 同步模式的「线程数 = 并发数」带来调度开销

- 验证：矩阵里 `threads_peak` 与 QPS 的关系
- 实测：sync c=1→8：线程 13→26，QPS 351→585（+67%）；c=32→64：线程 50→49，
  QPS 322→449。**线程数不是线性增长的**，因为 `ThreadPoolExecutor` 复用；
  且 QPS 在 c=32 反而下降（322 < 585）——这是**同一 Engine 的排队**，不是线程开销。
- 结论：**不成立**；真正的现象是「同一 Engine 上并发超过某点后 p99 恶化」，
  见 H5。

### H5 同一 Engine 上的并发会排队（HttpCache / socket pool 串行化）

- 验证：`--single-url` 对照 + `Session.stats` 的 `builds`/`hits`
- 实测：每个请求不同 URL 时 c=8 达到 585 req/s；c=32 掉到 322 且 p99 从 25 ms
  升到 60 ms。加并发反而更慢，是**同一 Engine 内部的串行化**在起作用。
- 结论：**成立**。缓解手段是增加 Engine 数（不同 per-request 配置命中不同 slot，
  上限 `max_engines`），而不是加线程。
- 注意反向陷阱（`docs/LESSONS.md` 29）：**每次请求换配置**会让每个请求重建一个
  Chromium 上下文。实测 `config-rotation`（`max_engines=1` + 交替 `verify`）：
  40 个请求产生 41 次 build、40 次 eviction。

### H6 大 body（4 MiB）的成本在分块重组

- 验证：同一 4 MiB 用三种方式发送/消费，并对比 `cache=True/False`
- 实测（顺序，同一 4 MiB，五种发送/消费方式；p50 of 5）：

  | 形态 | cache=True | cache=False |
  | --- | --- | --- |
  | Content-Length 4 MiB（一次发完） | 75.8 ms | 44.8 ms |
  | chunked 64 × 64 KiB | 82.8 ms | 47.2 ms |
  | chunk 大小 4 KiB / 16 KiB / 64 KiB / 256 KiB | — | 49.1 / 37.2 / 44.2 / 41.2 ms |
  | `iter_content` 消费块 4 KiB / 64 KiB / 1 MiB | — | 47.2 / 37.6 / 40.9 ms |
  | 并发 16 × 4 MiB chunked（总耗时） | 907 ms | 433 ms |

- 结论：**分块重组不是瓶颈**（同 cache 设置下只比 Content-Length 贵 9%；chunk 从
  4 KiB 换到 256 KiB 只差 8 ms）；**内存 HttpCache 才是**——同一个 body，开缓存贵
  69–109%（并发 16 时 907 ms vs 433 ms）。缓存对「同一 body 反复下载」是纯亏损：
  需要离线复用就保留，需要吞吐就 `cache=False`。
- 验证：`Session(cache=False)` 后重跑 `chunked-4m` 格，p50 应降到一半。

### H7 故障路径会挂住或泄漏描述符

- 验证：`tools/load-matrix.py` 的 `faults` 段（每故障 5 次，带 deadline，
  记录 `fd_before/after` 与结果分类）
- 实测：

  | 故障 | 结果 | fd 增量 | p50 |
  | --- | --- | --- | --- |
  | stall（不回包） | `Timeout` ×5 | +2 | 10011 ms（= deadline） |
  | reset（RST） | `ConnectionError` ×5 | 0 | 2.2 ms |
  | 截断 body | `IncompleteRead` ×5 | 0 | 2.9 ms |
  | 拒绝端口 | `InvalidURL` ×5（ERR_UNSAFE_PORT） | 0 | 1.0 ms |
  | 503 | `HTTP 503` ×5 | +1 | 2.0 ms |
  | 重定向 50 跳 | `TooManyRedirects` ×5 | 0 | 21.5 ms |

- 结论：**全部按 deadline 终止，没有挂起，没有描述符泄漏**。`+2` 是 fixture 侧
  新连接进入池，不是泄漏（跑第二轮不叠加）。

### H8 GC 是吞吐瓶颈

- 验证：`gc.get_stats()` + 矩阵里 RSS 的锯齿
- 实测：Python 侧每请求分配很小，整进程 RSS 在 `sync-get` 200 请求内 +0.03 KiB/请求
  （= 噪声）。**但 GC 是泄漏的掩盖者**——见 §5。

---

## 5. 内存泄漏的排查路径

### 5.1 通用路径（先分清「泄漏」与「缓存」）

1. **先要一条曲线，不是一个点。** 跑 5–8 批，每批等量请求，每批后记录
   RSS / fd 总数 / fd 种类 / 线程数。
2. **用后半程斜率判定。** 缓存会收敛（前几批涨、之后平），泄漏不会（一直涨）。
   `tools/leak-hunt.py` 同时报告全程斜率和后半程斜率，判据只看后半程。
3. **要求复现。** 单条序列的正斜率可能只是一次抖动：实测 `async-gather` 一次报
   `grows:fd`，下一次是 `9,9,9,9,9,9,9,9,9`。所以 `--repeat 2`，只有全部运行
   一致才算增长。
4. **末点尖峰不是泄漏。** 采样点正好落在「socket 还没关完」的瞬间，会让最后一点
   抬高一档：实测 `async-gather` 出现 `9,9,9,9,9,9,15`，最小二乘读成
   0.09 fd/请求并判定为泄漏。处理办法是**对相邻两点取最小值**再拟合——泄漏会抬高
   地板，单点尖峰不会。这条改动之后同一形态三次运行都是 `flat`，而人为恢复缺陷
   仍然被判定为 `fd/req=1.0, rss/req=522 KiB`（变异测试见 §5.3）。
5. **按 fd 种类归因。** 「多了 8 个 fd」不是结论；「多了 8 个 socket」才指向连接池，
   「多了 8 个 dev-shm」指向共享内存缓冲。
6. **分路径测。** 泄漏是**按路径**的。测试套件里测到的那条正好是唯一干净的那条，
   是最坏情况。`leak-hunt` 覆盖 11 条：`sync-get`、`sync-stream`、
   `sync-abandon`、`sync-error`、`sync-timeout`、`async-gather`、
   `async-abandon`、`session-churn`、`churn-nogc`、`config-rotation`、
   `discard-cookies`。
7. **把 GC 关掉再测一次。** 这是本次找到真凶的关键一步：引用环会被循环回收器
   周期性收掉，于是曲线呈锯齿、斜率接近 0，看起来「平」。`gc.disable()` 之后
   同一个循环变成一条直线。
8. **抓引用链。** `gc.get_objects()` 找到目标对象，`gc.get_referrers()` 逐层向上，
   直到看到一个属于 session/cache 的容器。
9. **最小复现。** `tools/leak-hunt.py --repro <shape>` 直接生成一个自包含脚本
   （自带服务器、自带 fd 种类打印），脱离 harness 也能跑。

### 5.2 本次定位到的两个缺陷（已修）

两个缺陷叠在同一个用户可见症状上：**反复 `Session() → get() → close()` 会让
描述符和 Chromium 上下文持续增长**。

#### 缺陷 A：`_SessionJarSink` 强引用 session，构成引用环

```
Session ──cookies──▶ RequestsCookieJar ──_sink──▶ _SessionJarSink ──_session──▶ Session
```

处于环中的对象**引用计数永远不为 0**。`Session` 自己没有 `__del__`
（`Session.__del__ is None`，已实测），它的资源是**属性**：`_engines`（EngineCache）
→ `EngineSlot` → `PyEngine` → Chromium `URLRequestContext` → socket 池。引用计数
不归零，这条链就一直被持有，socket 也就一直开着，直到循环回收器访问到那一代。

实测（`gc.disable()`，200 次 create/request/close）：

```
sockets at start=2  gc disabled
after 200 sessions with gc DISABLED: sockets=403 (+401)
after gc.collect(): sockets=3
```

修复：`_SessionJarSink` 用 `weakref.ref` 持有 session，并新增 `_owner()` 解析
（session 已被回收时抛 `SessionClosed`，与 `BaseSession._ensure_open` 的语义一致）。

#### 缺陷 B：`close()` 没有释放 `_seeded_slots` 里的强引用

`close()` 清空了 `EngineCache`，但 `_seeded_slots`（记录「哪些 slot 已经用 jar 播种
过」的 set）是对同一批 `EngineSlot` 对象的**第二份强引用**，而它只在**出现一个尚未播种的
slot**时（`_seed_store_from_jar` 的未命中分支）才被 `_prune_seeded_slots` 修剪。

实测（`gc.disable()`，60 次循环）：

```
unpatched : sockets +121  engines +60
patched   : sockets +0    engines +0
```

修复：`close()` 里 `self._seeded_slots.clear()`。语义安全：session 关闭后不会再
发请求，播种记录没有读者。

#### 修复前后对照（`tools/leak-hunt.py`）

| 形态 | 修复前 | 修复后 |
| --- | --- | --- |
| `churn-nogc`（GC 关闭，20/批×6 批） | fd **+1.0/请求**，RSS **+522 KiB/请求** | fd 0.0，RSS 0.2 KiB（后半程） |
| `session-churn`（GC 开启） | 靠 GC 掩盖，看似平 | 平，且不依赖 GC |
| `sync-get`/`sync-stream`/`sync-abandon` | 平 | 平 |
| `async-gather`/`async-abandon` | 平 | 平 |
| `config-rotation` | 平 | 平 |
| `discard-cookies` | 平 | 平 |

**为什么原来的门禁没抓到**：`tools/leak-probe.py` 只比较两批等量请求的差值，而
GC 会在两批之间把环收掉；`test_async_requests_do_not_leak` 只覆盖异步回调一条
路径。两条门禁都是对的，但它们都没有关掉 GC，也没有覆盖「反复创建/关闭 session」。

### 5.3 回归测试（含变异验证）

`bindings/python/tests/test_resource_release.py` 三条：

| 测试 | 断言 | 变异后 |
| --- | --- | --- |
| `test_close_empties_the_seeded_slot_set` | close 后 `len(_seeded_slots)==0` | 去掉 clear → `AssertionError: 1 != 0` |
| `test_a_closed_session_is_collectable_without_the_cyclic_collector` | `weakref` 在 `del` 后为 None | 恢复强引用 → `AssertionError: <Session …> is not None` |
| `test_churn_with_the_collector_disabled_does_not_grow` | 60 次循环 fd 增长 ≤ 8 | 同上两处变异 → 失败 |

AGENTS.md 第 6 节要求「没做变异测试的检查不算检查」，三条都做了。

---

## 5.4 Client / AsyncClient：别名不是免检的理由

```python
Client = Session           # sessions.py:3127
AsyncClient = AsyncSession # sessions.py:3128
```

两个名字是**同一个对象**（`cc.Client is cc.Session` 为 `True`），所以「别名和类会不会
行为不一致」这个问题在定义层面不存在。但**通过别名调用的那些功能**仍然可能错，而最初的
泄漏测试只用了 `Session`/`AsyncSession`，别名一条都没跑。补测后又找到两处真实缺陷
（记作第三、第四处，前两处在 §5.2）。

### 补测覆盖（16 条，`tests/test_client_alias.py`）

| 组 | 覆盖 |
| --- | --- |
| 同一性 | `Client is Session`、`AsyncClient is AsyncSession` |
| 生命周期 | `with`/`async with` 退出后 `_closed`；`close()` 幂等；关闭后 `get()` 抛 `SessionClosed`；关闭后**在飞的流仍能读完** |
| 并发 | 8 线程共享一个 `Client`（0 错误）；`close()` 与并发流量竞态（无线程挂起）；`max_clients` 在**服务端**观测到的峰值 |
| 异步 | 事件循环复用（两次 `asyncio.run`）；`cancel()` 后会话仍可用；`aiter_content`/`aiter_lines`；`async with` 中关闭 |
| 资源 | `Client`/`AsyncClient` 各 40 次 create/get/close（GC 关闭）后 engines/fd 不增长 |
| 协议面 | 9 个 verb、body/json/params/headers、重定向链与逐跳 cookie、`iter_lines`、`response.json()`、hooks、`max_response_bytes` |
| 类型存根 | `Session` 拒绝 `max_clients`；`AsyncSession`/`async_session()` 接受；`SessionOptions` 不再列 `max_clients` |

### 第三处缺陷：每个重定向跳共享同一个 `PreparedRequest`

`Response.history` 在 requests 里是「每次跳转的响应 + 产生它的请求」，调用者据此判断
哪一跳设了哪个 cookie。原实现把调用者的 `prepared` 直接赋给每一跳：

```
hops=3
  final.req.url  = /redirect?n=3
  hist[0].req.url = /redirect?n=3
  hist[1].req.url = /redirect?n=3     <-- 错：这一跳实际请求的是 n=2
  hist[2].req.url = /redirect?n=3     <-- 错：n=1
```

修复：每跳 `hop.request` 用上一跳的 `copy()` 并把 `Location` 应用上去（`sessions.py`
的 `_finalize`）。修复后逐跳 URL 正确：

```
hist[0].req.url = /redirect-cookie?n=3
hist[1].req.url = /redirect-cookie?n=2
hist[2].req.url = /redirect-cookie?n=1
```

**为什么只跑一跳的测试抓不到**：一跳时「共享的请求」和「本跳的请求」URL 相同，
断言恒真。回归测试因此用 **两跳**；变异测试确认——把 `hop.request` 改回共享，
`test_history_requests_differ_per_hop` 立刻失败。

### 第四处缺陷：类型存根宣称 `Session` 接受 `max_clients`

`SessionOptions`（`Session` 与 `AsyncSession` 共用的 TypedDict）列了 `max_clients`，
但只有 `AsyncSession` 有这个参数：

```
Session(max_clients=4)            -> TypeError: BaseSession.__init__() got an
                                     unexpected keyword argument 'max_clients'
chrome_client.session(max_clients=4) -> 同样的 TypeError
```

按 AGENTS.md §7「文档里的可验证事实必须与代码一致」和 §8「无法实现的选项必须抛错」，
这是存根说谎而不是运行期缺陷，所以修的是存根与生成器：

- `_types.pyi` 的 `SessionOptions` 删掉 `max_clients`，并在 docstring 写明它为什么不在；
- `tools/expand-verb-signatures.py` 里 `async_session` 的签名原来只由
  `BaseSession.__init__` 推导，于是**也漏了** `max_clients`；现在给它显式补上这一项，
  并注明理由。`--check` 仍然通过（幂等）。

### 一个**曾经被我判错**的文档说法（结论：文档成立，措辞已收紧）

第一轮我用 `/concurrency?ms=200` 测「`stream=True` 是否逃出 `max_clients` 的门」，
服务端观测峰值是 1，于是判定 `COMPATIBILITY_BOUNDARY.md` 写错了。**那个测法本身是错的**：
该端点是先 sleep 再发 header，客户端在 header 到达前一直卡在门里，当然看不到 body 阶段的
并发。

换成「立刻发 header、然后慢速滴 body」的 `/slowbody`，并让服务端计数器覆盖
**整个响应**（header + body），用服务端峰值作判据：

| 场景（`max_clients=1`，两个 0.4 s body） | 服务端峰值 | 墙钟 |
| --- | --- | --- |
| `stream=False` | 1 | 0.78 s |
| `stream=True` + 完整消费 body | **2** | 0.39 s |

所以原文的**结论成立**：请求在 header 处返回并把门释放，body 的后续读取在门外。
但原文措辞把「请求」与「body 读取」混成一句，读起来像「流式请求不受限」，而且没有说
清楚两种模式的分界点。已在 `docs/COMPATIBILITY_BOUNDARY.md` 改成按模式分开陈述：

- 门在**准入**时获取、在 facade **停止等待该请求**时释放；
- `stream=False`：等到整个响应到齐才释放 → 端到端受限；
- `stream=True`：header 到齐即释放 → 只有 header 阶段受限。

同时对齐了三处载体（AGENTS.md §6 的「扩展扫描范围」）：

| 载体 | 改动 |
| --- | --- |
| `docs/COMPATIBILITY_BOUNDARY.md` | 按模式分开陈述 + 给出测法与门禁文件 |
| `README.md` / `README.en.md` | 「限制同时在途的请求数」→「限制**同时被准入的请求数**」，并说明两种模式的分界 |
| `bindings/python36/README.md` | 逐字节镜像 `README.md`（门禁要求） |

### 这条结论现在有门禁

新增 `bindings/python/tests/test_max_clients_gate.py`（4 条），服务端计数器覆盖整个响应：

| 测试 | 断言 | 变异后 |
| --- | --- | --- |
| `test_session_does_not_accept_max_clients` | `Session(max_clients=…)` 抛 `TypeError` | — |
| `test_buffered_requests_are_bounded_end_to_end` | 峰值 = 1 | 去掉 `gate.acquire()` → `AssertionError: 2 != 1` |
| `test_streaming_requests_are_bounded_only_up_to_the_headers` | 峰值 = 2 | 让流式也等整个 body → `AssertionError: two streaming requests deadlocked…` |
| `test_streaming_is_not_slower_than_serialised` | 墙钟 < 串行化 × 0.75 | 同上 |

**写这条测试时踩到的两个坑，都记在代码里**：

1. **计数器必须覆盖整个响应。** 只数 header 之前的阶段，任何模式都报 1，等于什么都没证
   ——这正是这条说法长期没人验证的原因。
2. **两次测量之间必须等 fixture 排空。** 上一格的流式响应还在 handler 里 sleep，
   它的计数会漏进下一格：实测让一个正确的峰值 1 被报成 3。
3. 断言「不串行化」时要给 `asyncio.wait_for` 加上限：门被错误地覆盖到 body 阶段时，
   gather 会**永久阻塞**，测试表现为 CI 挂住而不是失败。

## 6. 复现命令

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64
export PYTHONPATH=bindings/python

# 1) 矩阵（含对照组与故障注入），约 4 分钟
python3 tools/load-matrix.py --ladder 1,8,32,64 --requests 64 --mode both \
    --shapes small-1k body-64k chunked-4m delay-10ms --out docs/perf/matrix.json

# 2) 分层归因
python3 tools/prof-attr.py threads  --concurrency 8 --requests 120
python3 tools/prof-attr.py pyhot    --concurrency 8 --requests 160
python3 tools/prof-attr.py syscalls --shape small-1k --concurrency 16
python3 tools/prof-attr.py stacks   --concurrency 16 --seconds 20
python3 tools/prof-attr.py perf        # 报告本机 perf/eBPF 是否可用

# 3) 泄漏（11 条路径，每条重复 2 次，只有全部一致才算增长）
python3 tools/leak-hunt.py --requests 20 --batches 6 --repeat 2 \
    --out docs/perf/leak-after-fix.json

# 4) 最小复现生成（自带服务器，脱离 harness 可跑）
python3 tools/leak-hunt.py --repro churn-nogc --requests 20 --batches 5
python3 docs/perf/repro_churn_nogc.py

# 5) 回归测试
python3 -m unittest bindings.python.tests.test_resource_release -v
```

```bash
# 变异测试（确认门禁能被证伪）：把 sessions.py 里 weakref.ref(session) 改回 session，
# 或删掉 close() 里的 self._seeded_slots.clear()，重跑第 5 步，必须失败。
```

---

## 7. 本机不可用的测量（如实声明，不记为通过）

| 手段 | 状态 | 原因 | 有权限时怎么做 |
| --- | --- | --- | --- |
| `perf record` | 不可用 | `/proc/sys/kernel/perf_event_paranoid = 4` | `sudo sysctl -w kernel.perf_event_paranoid=1` 后按 `prof-attr.py perf` 打印的命令 |
| `bpftrace` | 不可用 | 非 root | `sudo bpftrace -e 'profile:hz:99 /pid == PID/ { @[ustack] = count(); }'` |
| `valgrind` / `massif` | 未安装 | — | 装好后 `valgrind --tool=massif` 跑最小复现 |
| `py-spy` / `memray` | 未安装 | — | `py-spy record -p PID`；`memray run` |
| gdb 直接 `attach` | 不可用 | `yama ptrace_scope=1`：只能 trace 自己的子进程 | 已改为 gdb **自己启动**工作负载（`prof-attr.py stacks`），无需特权 |
| 火焰图 | 不可用 | 依赖 perf | 同上 |
| `audit-abi-layout.py` | 失败 | 本机 gcc 缺 `cc1`（`gcc -print-prog-name=cc1` 返回 `cc1` 但不存在） | 装 `gcc` 完整包后重跑；这不是本次改动引起的 |
| `test_compat.test_process_pool` | **已可跑** | 早先的 `BrokenPipeError` 是本会话 shell 没有 `PATH`（`len(os.environ)==2`）导致子进程拉不起来，不是库缺陷 | 用 `/usr/bin/python3` 显式启动后 195 条全过（skipped=1） |
| `review-before-tag.py` 的子步骤 | 需显式 `PATH` | 同上：`os.environ` 无 `PATH` 时子进程找不到 `python3` | `PATH=/usr/local/bin:/usr/bin:/bin /usr/bin/python3 tools/review-before-tag.py …` |

替代方案已落地：`prof-attr.py threads`（wchan 归因）、`pyhot`（cProfile）、
`stacks`（gdb 自启动 + SIGALRM 中断，全线程 bt）、`syscalls`（strace -c）
四者在无特权环境下给出分层答案，已在本机全部跑通并产出上面的证据。

---

## 8. 结论

1. **吞吐**：1 KiB 场景 300–700 req/s（对照组 1000–2700 req/s），CPU 峰值 1.9 核，
   网络线程全程 `ep_poll`。瓶颈是「每请求的 Python↔Rust↔C++ 往返」，不是 IO 线程、
   不是锁、不是 facade 的 Python 代码。
2. **并发**：加并发到某个点之后 p99 恶化且 QPS 下降，原因是同一 Engine 内部串行化；
   要更多并行度就开更多 Engine，而不是加线程。反过来，每次请求换配置会重建上下文，
   这是更贵的错误。
3. **大 body**：分块重组不是瓶颈；**内存 HttpCache 是**（4 MiB 场景贵 69–109%）。
4. **故障路径**：6 类故障全部按 deadline 终止，无挂起、无描述符泄漏。
5. **泄漏**：找到并修复两个真实缺陷（引用环 + close 未释放强引用）。修复后
   `--repeat 2` 的 11 条路径**全部为平**（`docs/perf/leak-after-fix.json`），
   其中 `churn-nogc` 修复前是 fd +1.0/请求、RSS +522 KiB/请求。三条回归测试做过
   变异验证，工具本身也做过变异验证（恢复缺陷后必须判红）。
6. **Client / AsyncClient**：两者是 `Session`/`AsyncSession` 的**同一个对象**
   （`is` 为真），但补测通过别名调用的功能后仍找到两处缺陷：重定向每一跳共享同一个
   `PreparedRequest`（`history[i].request.url` 全是原始 URL），以及类型存根把
   `AsyncSession` 独有的 `max_clients` 写进了两者共用的 `SessionOptions`。两处都已
   修复并加了变异验证的回归测试（`tests/test_client_alias.py`，16 条）。
   资源面：`Client`/`AsyncClient` 各 40 次 create/get/close（GC 关闭）后 engines 与
   fd 均不增长，异步 teardown 路径同样干净。
7. **门禁缺口已补**：原 `leak-probe.py` 与 `test_async_requests_do_not_leak` 都
   不关 GC、都不覆盖 session 生命周期，所以这两个缺陷都没被拦住。`tools/leak-hunt.py`
   补上了这两点，并已作为 `leaks` 组的第二步接入
   `tools/review-before-tag.py`（`--only leak-hunt` 实测 PASS，11/11 flat，
   报告写入 `{release}/leak-hunt.json`）。
