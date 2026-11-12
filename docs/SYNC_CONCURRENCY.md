# 同步（阻塞）客户端的并发模型：瓶颈假设、压测、诊断与优化

本文只针对 `Session`/`Client` 的**同步阻塞**模型。异步侧（`AsyncSession`）的并发边界见
`docs/COMPATIBILITY_BOUNDARY.md` 与 `bindings/python/tests/test_max_clients_gate.py`；
两者共用同一条 Chromium 网络线程，所以有些结论相通，但等待方式不同，不能互相套用。

工具：`tools/thread-ladder.py`（线程阶梯 + 拐点）、`tools/sync-diag.py`（线程 dump /
锁等待 / 连接池曲线）、`tools/prof-attr.py`（wchan、strace、gdb 栈、cProfile）、
`tools/perf_harness.py`（线路级 fixture，服务端并发计数）。

---

## 0. 这个「同步」到底阻塞在哪里

必须先说清楚，否则下面每一条假设都会测错对象：

```
Python 调用线程
  └─ Session.get → prepare_request → _send_once → _send_on_slot
       └─ native.start_stream()             ← py.detach：释放 GIL
       └─ native.await_response()           ← Rust Condvar::wait_timeout(WAIT_SLICE)
            ↑ 被谁唤醒：Core 的 callback_runner → PyRequest 的 event_callback
  └─ read_body()                            ← 同上，另一个 Condvar 等待
```

三条由此推出的事实，后面全部依赖它们：

1. **没有客户端线程池。** 每个调用线程自己阻塞，没有「请求排队等 worker」这一层。
   所以「同步 IO 阻塞线程池、队列积压」在这个库里**不存在对应结构**——积压发生在
   调用方自己的线程池里，不在库里。
2. **阻塞期间释放 GIL**（`py.detach`），所以多线程能真正并行；瓶颈不在 GIL。
3. **只有一个网络 IO 线程**（`MiniCronetNet`，`core/source/engine.cc:253-276`），
   所有 Engine 共享。但它全程在 `ep_poll`，CPU 峰值 1.9 核（实测），不是饱和点。

---

## 1. 瓶颈假设清单与实测结论

每条都给出验证手段与本次实测值。**被证伪的同样列出来**——它们把优化预算从错误的
方向上拿回来。

### H1 线程数与 CPU 核数不匹配导致上下文切换开销 —— **不成立**

- 验证：`tools/thread-ladder.py` 每格读 `/proc/PID/status` 的
  `voluntary_ctxt_switches` / `nonvoluntary_ctxt_switches`，以及
  `/proc/PID/schedstat` 的 waiting 纳秒。
- 实测（8 核，1 KiB 响应，每格 64 请求）：

  | 线程 | QPS | 用了几个核 | 主动切换 | 被动切换 | sched 等待 |
  | --- | --- | --- | --- | --- | --- |
  | 1 | 363 | 0.91 | 15 | 3 | 0.7 ms |
  | 4 | 309 | 0.82 | 22 | 0 | 2.2 ms |
  | 8 | 489 | 1.53 | 50 | 6 | 6.2 ms |
  | 16 | 457 | 1.57 | 93 | 12 | 11.7 ms |
  | 32 | 482 | 1.81 | 115 | 17 | 8.3 ms |

- 判读：32 线程时 CPU 只用了 1.81 核（8 核的 23%），**被动切换只有 17 次**。
  真正的上下文切换风暴表现为「CPU 打满 + 被动切换暴涨 + QPS 下跌」，三者一个都没出现。
- 结论：**不成立**。线程数不是瓶颈，因为线程大部分时间在 `futex` 上睡着，不参与调度竞争。

### H2 同步 IO 阻塞线程池、队列积压 —— **结构上不成立**

- 这个库没有内部线程池，调用线程自己阻塞。要测的是另一件事：**一个卡住的流式消费者
  会不会拖住同一个 Engine 上的其它请求**（这正是 v7 的线上故障，见
  `docs/PROJECT_STATUS.md` 阶段 1）。
- 验证：一个线程开 `stream=True` 读一块后停住，8 个线程同时打小请求，对照组不停。
- 实测：

  | 场景 | QPS | p50 | p99 |
  | --- | --- | --- | --- |
  | 有卡住的流 | 688 | 9.7 ms | 21.5 ms |
  | 无卡住的流（对照） | 656 | 10.2 ms | 21.6 ms |

- 结论：**不成立**（ABI v8 的每请求独立 callback runner 已经修掉了这个故障）。
  积压如果出现，只会出现在调用方自己的线程池里。

### H3 锁竞争（`_cookie_lock` / `EngineCache._lock`） —— **锁本身不是，但锁里做的事是**

- 验证：`tools/sync-diag.py locks` 把 facade 的两把 RLock 换成计时包装，统计每次获取的
  等待时间与「占线程时间比」。
- 实测（16 线程 × 200 请求）：

  | 锁 | 调用次数 | 累计等待 | p99 | 占线程时间比 |
  | --- | --- | --- | --- | --- |
  | `_cookie_lock` | 403 | 4.7 ms | 0.049 ms | 0.001 |
  | `EngineCache._lock` | 203 | 2.8 ms | 0.069 ms | 0.001 |

- 结论：**锁获取本身不成立**（占比 0.1%）。但顺着这把锁往下挖，发现了一个真实的
  性能缺陷——**锁里的工作量**随会话 cookie 数线性增长：见 H3b。

### H3b（本次发现并修复）每请求重复把整个会话 jar 写回 cookie store

`prepare_request` 无条件把会话 jar 合并成一个新 jar（`merge_cookies`），
`_resolve_cookies` 再把这个合并结果里的**每一个** cookie 写回引擎 store。store 本来
就持有它们（ABI v9 起 jar 是 store 的视图），所以这些写入全是空转，但每次都要拿锁、
每次都要一次 store 调用。

实测（一次请求，warm）：

| 会话 cookie 数 | 修复前 write_through | 修复前耗时 | 修复后 write_through | 修复后耗时 |
| --- | --- | --- | --- | --- |
| 0 | 0 | 17.3 ms | 0 | 12.9 ms |
| 20 | 60 | 20.8 ms | 20 | 11.3 ms |
| 200 | 600 | 172.5 ms | 200 | 82.2 ms |

8 线程并发（warm，96 请求）：

| 场景 | 修复前 QPS | 修复后 QPS |
| --- | --- | --- |
| 空 jar | 537 | 581 |
| 200 cookie | **17** | **352** |
| 200 cookie + 每请求换配置 | 9 | 399 |

修复：只有在调用方**真的传了 `cookies=`** 时才构造合并视图；每请求写回只写调用方
新增的那些。requests 兼容面不变（`prepared._cookies` 仍是被请求时的合并视图，否则是
空 jar）。

回归测试：`bindings/python/tests/test_cookie_request_cost.py`（8 条，含变异验证）。

- 剩余开销：store 里 cookie 数从 0 涨到 177 时，单请求 p50 从 2.16 ms 涨到 3.88 ms。
  这是 Chromium `CookieMonster` 对每个请求做域名/路径匹配的固有成本，不是绑定层能消掉的；
  用同样大小的填充头（2 KiB `X-Pad`）对照，p50 仍是 2.00 ms，说明差距来自 cookie 匹配
  而不是请求头变长。

### H4 连接池耗尽（maxTotal / maxPerRoute） —— **成立，而且是首要瓶颈**

- 本库没有 `maxTotal`/`maxPerRoute`；对应物是 **Chromium 的每 host 组 6 条 HTTP/1.1 连接**
  上限，以及「一个 Engine 一整个 `URLRequestContext`」的 socket 池。
- 验证：`tools/sync-diag.py pool-curve`，服务端观测**同时在飞**的请求数（不是客户端
  自己数的连接数——客户端无法见证自己的天花板）。
- 实测（50 ms 延迟端点，每格 2×线程数请求）：

  | 线程 | 服务端峰值 | QPS |
  | --- | --- | --- |
  | 1 | 1 | 23.1 |
  | 3 | 3 | 61.1 |
  | 6 | **6** | 124.0 |
  | 8 | 6 | 120.1 |
  | 12 | 6 | 130.9 |
  | 16 | 6 | 84.5 |

- 结论：**成立**。峰值在 6 处封顶，线程从 6 加到 16 吞吐不再上升，p99 反而恶化。
- **怎么突破**（两条都要实测过）：

  | 手段 | 服务端峰值 | 说明 |
  | --- | --- | --- |
  | 单 Engine，12 线程 | 6 | 基线 |
  | 两个 host（两个端口），12 线程 | **24** | 每 host 组各 6 条 |
  | 4 个不同 per-request 配置（4 个 Engine），24 线程 | **24** | 每个 Engine 一个池 |

  注意第二行是 24 而不是 12：两个 host 组各 6 条 = 12 条连接，但服务端看到的是同时在飞
  的**请求**数，`max_engines` 与连接复用叠加后可以更高。这里只陈述实测值。

### H5 DNS 同步解析阻塞 —— **不成立**

- 验证：同一台 fixture，分别用 `127.0.0.1` 与 `localhost`（走 resolver），单线程与
  8 线程各 64 请求。
- 实测：

  | 目标 | 线程 | QPS | p50 | p99 |
  | --- | --- | --- | --- | --- |
  | 127.0.0.1 | 1 | 423.8 | 2.06 ms | 3.49 ms |
  | 127.0.0.1 | 8 | 559.1 | 10.41 ms | 27.66 ms |
  | localhost | 1 | 408.7 | 2.10 ms | 3.92 ms |
  | localhost | 8 | 622.4 | 9.61 ms | 18.61 ms |

- 结论：**不成立**。解析由 Core 自己的 `HostResolver` 完成
  （`net::HostResolver::CreateStandaloneResolver`，`core/source/engine.cc:847`），
  跑在 Chromium 的线程池上，不占用调用线程；有 DNS 缓存，重复请求不重新解析。

---

## 2. 压测方法：线程阶梯与两个拐点

`tools/thread-ladder.py` 每格记录：QPS、p50/p90/p99、p99/mean、服务端峰值、
上下文切换、sched 等待、CPU 核数、fd 峰值。**拐点由数据算，不由人眼看**：

- **吞吐拐点**：线程翻倍但 QPS 增幅 < 20% 的第一个台阶；
- **延迟拐点**：p99/mean 比值超过基线 1.5 倍的第一个台阶。

用比值而不是绝对 p99，是因为延迟主导的形态在每一格都有很大的 p99，绝对值无法跨形态比较。

实测（8 核，`tools/thread-ladder.py`，1 KiB 响应，64 请求/格）：

| 形态 | 线程 | QPS | p50 | p99 | 服务端峰值 | 主动切换 | sched 等待 | CPU 核 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| delay | 1 | 41 | 22.9 | 31.3 | 1 | 21 | 3.7 ms | 0.15 |
| delay | 2 | 82 | 23.2 | 26.2 | 2 | 23 | 3.1 ms | 0.23 |
| delay | 4 | 147 | 24.3 | 30.1 | 4 | 24 | 1.8 ms | 0.41 |
| delay | 6 | 215 | 23.3 | 30.3 | **6** | 38 | 8.2 ms | 0.60 |
| delay | 8 | 221 | 29.3 | 40.0 | 6 | 42 | 3.6 ms | 0.69 |
| delay | 12 | 215 | 46.7 | 52.9 | 6 | 55 | 5.7 ms | 0.64 |
| fast | 1 | 274 | 3.3 | 6.5 | 1 | 17 | 1.3 ms | 0.77 |
| fast | 2 | 433 | 3.8 | 7.3 | 1 | 24 | 1.7 ms | 1.01 |
| fast | 4 | 358 | 8.8 | 22.4 | 2 | 24 | 9.9 ms | 1.01 |
| fast | 6 | 399 | 9.1 | 22.5 | 3 | 47 | 15.5 ms | 1.18 |
| fast | 8 | 645 | 9.5 | 23.9 | 3 | 45 | 3.2 ms | 1.92 |
| fast | 12 | 633 | 14.4 | 29.3 | 4 | 59 | 5.1 ms | 1.98 |

**延迟主导形态**：QPS 在 6 线程处封顶（6→8 只涨 3%），服务端峰值同时锁在 6，之后 p50
从 23 ms 涨到 47 ms。这是连接池限制的典型形状：**吞吐封顶 + 延迟线性恶化 + 服务端峰值
不变**。拐点算法给出 `6 → 8, gain 1.03`。

**快响应形态**：数据不单调（4 线程比 2 线程低），拐点算法报 `2 → 4, gain 0.83`。这个数字
**不能当成结论**：快响应的单格样本量小、噪声大，服务端峰值只有 1–4（请求来不及重叠）。
它说明的是「这个形态测不出稳定的拐点」，而不是「2 线程最优」。要判断连接池必须用延迟
主导的形态。

两个形态共同说明的一点：**CPU 全程没有超过 2 核**（8 核机器），sched 等待始终在 10 ms
以内。真正的上下文切换风暴不会是这个样子。

**阶梯的两个陷阱**（都在代码注释里）：

1. 每格之前必须把 fixture 的计数器 reset 并等它排空，否则上一格的峰值会漏进来。
2. 快响应形态看不出连接池天花板——必须换延迟主导的端点，否则会把「请求来不及重叠」
   误读成「连接池只有 2 条」。

---

## 3. 诊断手段（jstack / visualvm 的对应物）

| Java 生态 | 本库 | 命令 |
| --- | --- | --- |
| `jstack` | 全线程 Python 栈采样 | `tools/sync-diag.py dump --threads 8` |
| VisualVM 锁视图 | facade 锁等待统计 | `tools/sync-diag.py locks --threads 16` |
| 连接池活跃/等待曲线 | 服务端并发峰值曲线 | `tools/sync-diag.py pool-curve --ladder …` |
| JFR / async-profiler | `perf`（需权限）/ gdb 栈 | `tools/prof-attr.py perf` / `stacks` |
| 线程状态 | wchan + CPU ticks | `tools/prof-attr.py threads` |
| 系统调用 | `strace -c` | `tools/prof-attr.py syscalls` |
| 锁内热点 | cProfile | `tools/prof-attr.py pyhot` |

`dump` 的实测输出（8 线程 × 4 次采样，每线程取栈顶）：

```
count  top frame
28     sessions.py:2614 in _send_on_slot     <- 阻塞在 Core 调用里，正常
17     perf_harness.py:248 in _respond       <- fixture 线程，不是被测对象
6      perf_harness.py:88 in _read_request
4      selectors.py:415 in select            <- fixture 的 accept 循环
```

判读规则：栈顶在 `_send_on_slot` / `await_response` / `start_stream` 是**正常的同步
阻塞**；栈顶在 `_cookie_lock` / `_lock` 上面才是**facade 锁等待**，那种等待加线程只会更糟。

---

## 4. 优化方向（按实测收益排序）

| 方向 | 实测结论 | 建议 |
| --- | --- | --- |
| **连接池 / 并行度** | 每 host 6 条是硬顶；多 host 或多 Engine 能线性放大（6 → 24） | 需要更高单 host 并发：上 HTTP/2；多目标：多 host 天然并行；需要更多独立池：不同 per-request 配置命中不同 Engine（`max_engines` 要 ≥ 用到的配置数） |
| **cookie 开销** | 已修：200 cookie 场景 17 → 352 req/s | 长会话里别囤积无用 cookie；`discard_cookies=True` 会清空 store，代价已知 |
| **每请求换配置** | 每次重建一个 `URLRequestContext`（实测 40 请求 41 次 build） | 别把「按请求切换的配置」当免费参数（`docs/LESSONS.md` 29） |
| **线程池参数化** | 本库无内部线程池；拐点由连接池与 Engine 数决定 | 调用方线程数配到「每 host 6 × host 数 × Engine 数」附近即可，再高只增 p99 |
| **超时与重试退避** | 6 类故障全部按 deadline 终止，无挂起、无 fd 泄漏 | 一定要设 timeout；`RetryStrategy` 有 backoff/jitter，别用固定间隔 |
| **批量合并请求** | 未测（本库没有批量 API） | 需要批量的场景应在服务端做，或上 HTTP/2 多路复用 |
| **局部异步化（同步→异步边界）** | 同步/异步共用同一网络线程，异步不增加并行度，只减少线程 | I/O 密集且线程数远超连接数时换 `AsyncSession`；CPU 密集时同步线程池更合适 |

### 同步→异步的边界怎么划

判据不是「哪个更快」，而是**线程在等什么**：

- 线程 90% 时间阻塞在 `await_response`（实测 `stacks` 子命令可见）：等待占主导 → 换
  `AsyncSession`，用协程替代线程，省下的是**线程栈与调度**，不是网络时间；
- 线程大量时间在 Python 侧（`pyhot` 里 facade 占比高）：换异步没有收益，那是 CPU 工作；
- 线程数已经接近「每 host 6 × host 数」：再多的线程只是在连接池前排队，换异步同样无效，
  该做的是加 host 或上 HTTP/2。

---

## 5. 复现命令

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64
export PYTHONPATH=bindings/python

# 线程阶梯 + 两个拐点（含上下文切换 / sched 等待 / 服务端峰值）
python3 tools/thread-ladder.py --ladder 1,2,4,6,8,12,16 --requests 96 \
    --shapes fast delay --out docs/perf/thread-ladder.json

# 连接池天花板曲线（延迟主导形态，否则看不出重叠）
python3 tools/sync-diag.py pool-curve --ladder 1,3,6,8,12,16

# facade 锁等待
python3 tools/sync-diag.py locks --threads 16 --requests 200

# 线程栈采样（jstack 对应物）
python3 tools/sync-diag.py dump --threads 8 --samples 6

# 分层归因
python3 tools/prof-attr.py threads --concurrency 8 --requests 120
python3 tools/prof-attr.py pyhot   --concurrency 8 --requests 160
python3 tools/prof-attr.py syscalls --shape small-1k --concurrency 16
python3 tools/prof-attr.py stacks  --concurrency 8 --seconds 20

# cookie 开销回归
python3 -m unittest bindings.python.tests.test_cookie_request_cost -v
```

---

## 5.5 优化实施记录：缓存 .netrc 路径展开

H1/H3 的实测把 `prepare_request` 拆开之后，最贵的一项不是锁、不是 cookie，而是
**每请求一次的 `.netrc` 路径解析**。

### 问题

`get_netrc_auth` 每请求都跑一遍：

```python
for candidate in candidates:            # ~/.netrc, ~/_netrc
    path = os.path.expanduser(candidate)
    if os.path.exists(path):
        break
```

`expanduser` 在 `HOME` 未设置时会退到 `pwd.getpwuid`——**容器和 CI 里这是常态**，
本会话也是（`HOME=None`）。实测：

| 场景 | `expanduser` 单次 | `get_netrc_auth` 单次 |
| --- | --- | --- |
| `HOME` 未设置 | 0.047 ms | **0.15 ms** |
| `HOME` 已设置 | 0.0009 ms | 0.0014 ms |

每请求 0.15 ms，占 2.08 ms 请求的 7%；在异步侧，它占每个协程同步前缀的 0.92 ms
（前缀总共 1.15 ms）。

### 修改

只缓存**展开结果**，不缓存存在性检查：

```python
_netrc_candidates_cache = {"key": _UNSET, "paths": None}

def _netrc_candidates():
    key = (os.environ.get("NETRC"), os.environ.get("HOME"))
    cached = _netrc_candidates_cache
    if cached["key"] == key:
        return cached["paths"]
    ...展开并缓存...
```

两个设计决定，都有测试守着：

1. **只缓存展开，不缓存 `stat`。** 一次 `stat` 约 0.005 ms，而它是进程运行期间
   **会变**的部分。把存在性也缓存掉，等于让运行中新建或删除的 `.netrc` 永远不被发现。
2. **`_UNSET` 哨兵而不是 `None`。** 第一版把「查过了，没有文件」记成 `path=None`，
   于是这个状态和「还没查过」无法区分——**最常见的无 `.netrc` 场景反而每请求重查**。
   实测那一版 `_netrc_path` 仍是 0.15 ms/次，等于没优化。这是本次唯一一次
   「改了但没生效」，靠直接测量 `_netrc_path` 单次耗时才发现。

### 效果（交错 A/B，各 6 轮）

| 指标 | 缓存前 | 缓存后 | 变化 |
| --- | --- | --- | --- |
| 单请求 p50（16 B body） | 2140 us | **1739 us** | **-18.7%** |
| 8 线程吞吐 | 661 req/s | **878 req/s** | **+32.9%** |
| `prepare_request` 本身 | 156 us | **38 us** | -75.5% |
| 异步同步前缀 / 协程 | 1.15 ms | **0.23 ms** | -80%（256 协程省 236 ms 循环时间） |

### 剩余开销的归属（优化后重新分解）

| 步骤 | p50 | 占请求 |
| --- | --- | --- |
| `start_stream`（原生，等响应头） | 1.12 ms | 51% |
| `read_body`（原生，等 body） | 0.56 ms | 26% |
| `prepare_request` | 0.067 ms | 3.1% |
| `_lease` | 0.054 ms | 2.5% |
| `_finalize` | 0.054 ms | 2.5% |
| 其余 | — | 15% |

**结论：Python 侧已经不再是瓶颈**，87.7% 是两次原生等待。继续压 Python 层的收益上限
是每请求 0.2 ms 左右。把两次等待合成一次需要 Core 提供「连 body 一起返回」的入口，
已作为 ABI 提案登记在 `docs/NEXT_STEPS.md`「提案：两项性能 ABI 改动」§二，
预期收益约 0.5 ms/请求（-24%）。

### 对照组：与 `http.client` 的差距

| 客户端 | p50（16 B） |
| --- | --- |
| `http.client` | 318 us |
| 本库（优化后） | 1739 us |

差距不是 Python 开销（38 us），而是**每请求两次跨线程唤醒**（网络线程 → 等待线程），
以及 Chromium 自己的请求生命周期。这是这个架构的固有成本，不是缺陷。

---


## 7. 线程池并发会不会死锁

结论：**在 32 种压测形状下都没有死锁**。但这不等于"随便用"，有三条真实边界，
其中一条是本项目历史上真实发生过的死锁。

### 7.1 为什么能共享一个 Session

同步路径阻塞时走 `py.detach` 释放 GIL，所以多个线程能真正并行等待；
`EngineCache` 用 `RLock` 包住查表；Engine 本身是 `Arc<EngineInner>` 且
`unsafe impl Send + Sync`（依据是 Core ABI 的线程安全契约）。

### 7.2 实测（`faulthandler` + 硬看门狗，挂住就 dump 而不是干等）

| 形状 | 结果 |
| --- | --- |
| `ThreadPoolExecutor(8/32/64)` 共享一个 Session | OK |
| 每线程一个 Session，32 worker × 64 task | OK |
| 卡住的流式消费者 + 8 worker 打同 Session（v7 那个 P0 的形状） | OK |
| 嵌套线程池（池内任务再开池） | OK |
| 16 worker 在飞时 `close()` | OK，无线程残留 |
| 16 worker × 64 个 stream/abandon 任务 | OK |
| 5 轮 × 32 worker × 128 task | OK，fds 5 → 17 后稳定 |
| 16 worker × 32 个全部超时的请求 | OK |
| `max_engines=1` + 每请求换配置，16 worker | OK |
| 4 worker × 200 排队任务 | OK |
| 同步线程池 + asyncio 循环同时跑 | OK |
| 阻塞的 `response` hook / 慢 `content_callback` | OK |
| hook 抛异常 | OK，会话事后仍可用 |
| 64 个 Session 分散在 16 worker | OK，fds +0 |

### 7.3 三条真实边界

**1. fork 会死锁——但已经变成快速失败。** 这是本项目历史上唯一的真死锁：
`Runtime` 是进程级单例，fork 后子进程继承了**已死**的 `ThreadPoolInstance` 与
`network_thread_`，而 `ThreadPoolInstance::Get()` 仍返回非空，于是不会重建；
任何网络调用永久阻塞。现在 C++ 侧有 `pthread_atfork` 守卫（
`docs/FORK_SAFETY_PATCH.md`），实测：

| 场景 | 结果 |
| --- | --- |
| fork **之前**没有 engine | 子进程正常，`ok 200` |
| fork **之后**子进程新建 Session | `RequestException: InitializationFailed`（快速失败） |
| fork **之后**子进程复用父 Session | `RequestException: InitializationFailed` |

所以进程池必须用 `spawn` 或 `forkserver`，这是 `test_process_pool` 断言的东西。

**2. GIL 与 Rust Mutex 的加锁顺序。** 也曾真实死锁过：`schedule_event` 在
edition 2021 下跨调用持有 `MutexGuard` 再抢 GIL，约 1/3 概率挂死（
`docs/PROJECT_STATUS.md`「其它已修复」）。当前实现是**先放锁再抢 GIL**——
`install_event_bridge` 里 `Python::attach` 之前不持有任何锁。

**3. 在事件循环里调用同步 API。** 这不会死锁，但会把整个循环卡住：实测三个连续
`Session.get()` 在协程里耗时 0.69 s，期间循环一次都没 tick。库会发一次
`RuntimeWarning`（`_warn_if_blocking_the_loop`），但拦不住——同步 API 必须阻塞。

### 7.4 怎么用才对

| 做法 | 判断 |
| --- | --- |
| 一个 Session 共享给线程池 | ✅ 支持，也是文档推荐的用法 |
| 每线程一个 Session | ✅ 也可，但会各自建 Chromium 上下文（更贵） |
| `ProcessPoolExecutor(mp_context=spawn)` | ✅ 必须显式 `spawn`/`forkserver` |
| 默认 `fork` 的进程池 | ❌ 子进程必然 `InitializationFailed` |
| 在协程里调 `Session.get()` | ❌ 卡住整个循环，改用 `AsyncSession` |
| 在 hook 里做长耗时同步工作 | ⚠️ 占住该 worker（不阻塞其它 worker），但拖慢该请求 |

线程数本身不需要超过「每 host 6 × host 数 × Engine 数」，再高只是排队（§4 H4）。

---

## 6. 结论


1. **首要瓶颈是每 host 组 6 条 HTTP/1.1 连接**，不是线程、不是锁、不是 DNS、不是 GIL。
   线程加到 6 之后 QPS 封顶、p99 继续恶化，是线程在连接池前排队。
2. **上下文切换不是问题**：32 线程时 CPU 只用 1.81 核，被动切换 17 次。
3. **锁获取不是问题**（占线程时间 0.1%），但**锁里做的事**曾经是：每请求把整个会话 jar
   写回 store，200 cookie 场景把 8 线程吞吐从 581 打到 17 req/s。已修复（352 req/s），
   并有 8 条回归测试 + 变异验证。
4. **DNS 不阻塞调用线程**：Core 自己的 `HostResolver` 跑在 Chromium 线程池上。
5. **同步模型没有内部线程池**，所以「线程池队列积压」在本库里没有对应结构；积压只会在
   调用方自己的池里。
6. **同步→异步的价值是省线程，不是提吞吐**：两者共用同一条网络线程，并行度上限由连接池
   与 Engine 数决定。
