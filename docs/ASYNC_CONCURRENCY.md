# 异步（事件循环）客户端的并发模型：瓶颈假设、压测、诊断与优化

本文只针对 `AsyncSession`/`AsyncClient`。同步侧见 `docs/SYNC_CONCURRENCY.md`。
两者共用同一条 Chromium 网络线程（`MiniCronetNet`），但等待方式完全不同：同步是
调用线程阻塞在 Rust `Condvar` 上，异步是把回调排进 asyncio 的事件循环。

2026-11-27 的 facade 层审计（头块构造、环境扫描、profile 头记忆化、异步响应字段）见
[PERF_AUDIT_0.2.20.md](PERF_AUDIT_0.2.20.md)；那份记录同时给出独立进程 fixture 的对照数据，
说明本文的历史绝对数为何偏低。

工具：`tools/async-diag.py`（lag / futures / slow-consumer / flood / tasks）、
`tools/perf_harness.py`（线路级 fixture）、`tools/prof-attr.py`（wchan / strace / gdb / cProfile）。

---

## 0. 唤醒路径

```
Core 网络线程 MiniCronetNet
  └─ 回调 → Rust set_event_callback 的闭包
       └─ notify_scheduled.swap(true) 去重
            └─ loop.call_soon_threadsafe(state.notify)      ← 跨线程投递
                 └─ 事件循环线程执行 notify()
                      └─ request.poll_events(ASYNC_POLL_BATCH=64)
                           └─ 逐事件分发；批次打满则 loop.call_soon(self.notify)
```

三个关键常量与事实（`sessions.py`）：

| 项 | 值 | 含义 |
| --- | --- | --- |
| `ASYNC_POLL_BATCH` | 64 | 每次唤醒最多取 64 个事件，打满则立刻重新排队 |
| `STREAM_BUFFER_LIMIT` | 1 MiB | 异步流式 body 的 Python 侧缓冲上限，超过即停止 `poll_events`；Rust 侧另有一层 1 MiB 的 `BODY_BUFFER_LIMIT`，两层合计每条停滞流 2 MiB |
| `max_clients` | 可选 | `asyncio.Semaphore`。**配额覆盖到哪一步取决于 `stream`**：`stream=True` 在响应头放行，`stream=False` 一直占到 body 收完（`sessions.py:4939-4950` 的 `gate.release()` 在 `_perform` 返回之后）。见 `test_max_clients_gate.py` |

**没有自带事件循环，也没有内部线程池**：协程跑在调用方给的 loop 上，网络 IO 在
Chromium 的网络线程上。所以「线程池调度饥饿」在这个库里同样没有对应结构。

---

## 1. 瓶颈假设清单与实测结论

### H1 事件循环被阻塞 —— **成立，而且是本次最重要的发现**

协程本身不是「非阻塞」的：`await` 之前的**同步前缀**跑在事件循环线程上，一个批次里
N 个协程的前缀会串行叠加成一次长时间的「循环停摆」。

- 验证：`tools/async-diag.py lag`。监控协程每 5 ms 醒一次并记录「晚醒多久」，
  这才能区分「服务器慢」和「回调占住了循环」。
- 实测（1 KiB，单次 `gather`，8 核）：

  | 请求数 | 一次循环停摆 | 前缀总和 |
  | --- | --- | --- |
  | 64 | 29–41 ms | 78 ms |
  | 256 | 77–137 ms | 323 ms |
  | 512 | 145–308 ms | 633 ms |
  | 1024 | — | — |

  停摆随请求数线性增长，前缀总和是它的上限（循环在两次停摆之间还有自己的活要干，
  所以观察到的停摆是下限、前缀总和是上界）。

- **前缀里 73% 是 `prepare_request`**：把 `prepare_request` 换成最便宜的等价实现
  重跑同一个批次，前缀从 294 ms 掉到 79 ms（256 协程）。

- 结论：**成立**。这是 asyncio 客户端最典型的病：单个请求 2 ms，但 512 个请求的
  「提交动作」本身要占住循环 300 ms，期间**进程里任何其它回调都得不到执行**。

### H2 回调地狱导致错误处理与超时缺失、请求悬挂 —— **不成立**

- 验证：`tools/async-diag.py futures` 统计批次结束后仍 pending 的 task；
  以及把 6 类故障同时打进去看是否都能终止。
- 实测（256 请求 / 64 并发）：`before tasks=1 → settled tasks=1`，**+0**。
- 6 类故障（stall / reset / truncate / refused / 503 / 重定向环）全部映射到具体异常
  类型并按 deadline 终止（见 `docs/PERF_TEST_AND_LEAK.md` §4 H7）。
- 结论：**不成立**。错误处理在 `_AsyncState` 一处收敛，超时由 `loop.call_later` 计时，
  不依赖调用方记得设 timeout。

### H3 无背压导致内存暴涨 / OOM —— **不成立（有背压）**

- 验证：`tools/async-diag.py slow-consumer`——服务端猛发、消费者每块睡 2 ms。
- 实测（16 MiB body，256 块）：

  ```
  RSS 增长（每 16 块采样）: 11108 → 14432 → 15488 → 16288 → 16288 → 16304 → ... → 16344
  ```

  前 4 个采样点爬到约 16 MiB 后**完全走平**（平台）。这 16 MiB 不是队列：队列上限
  是 1 MiB，其余是 Python 侧 `aiter_content` 的缓冲与分配器 arena。关键是**斜率归零**。

- 结论：**不成立**。`STREAM_BUFFER_LIMIT = 1 MiB` 生效：超过上限时 `notify()` 直接
  返回并置 `throttled`，Core 停止读取；消费者追上后恢复。

### H4 协程/线程池调度饥饿、任务队列无界 —— **部分成立**

- **线程池**：本库没有，协程直接跑在调用方 loop 上，所以「线程池饥饿」无对应结构。
- **任务队列无界**：成立，但它是 `asyncio.gather` 的性质，不是库的。`max_clients`
  只限制**在飞**请求数，不限制**已创建**的协程数。实测 `flood --requests 1024`：
  `tasks_peak = 1026`（1024 个请求 + 2 个内部任务），与 `--concurrency` 无关。
- 结论：**部分成立**。库侧的 `max_clients` 是准入闸门，但**提交**本身没有闸门；
  一次 `gather(10_000)` 会先创建 10000 个协程，每个都占一个 `_AsyncState`。

### H5 Future 未取消、资源泄漏 —— **任务不泄漏，socket 由 TCP 半关闭持有**

- 验证：`tools/async-diag.py tasks`，取消 N 个打向 `/stall` 的请求，看 task 数与 fd 数。
- 实测（32 次取消）：`tasks: 1 → 1`（**不泄漏**），`fds: 8 → 45`（**增长**）。
- 追查 fd 到底是什么：`ss -tanp` 显示全部是 `CLOSE-WAIT`，对端是 fixture。
  换成「服务端 400 ms 后应答、客户端 50 ms 超时」再测：

  ```
  before=7  at failure=13  settled(1.5s)=5     ← 回到基线以下
  ```

  即：**对端还握着连接时，客户端 socket 停在 FIN-WAIT-2/CLOSE-WAIT；对端一应答就释放**。
  这是 TCP 半关闭，不是库的泄漏。
- 结论：**任务不泄漏；fd 增长是对端未关闭时的正常半关闭状态**，只有当取消速度持续
  超过对端回收速度时才会变成真实的 fd 压力。这一条在 `test_async_model.py` 里被固定。

---

## 2. 压测方法

### 2.1 事件循环 lag（比吞吐更能说明问题）

`tools/async-diag.py lag`：监控协程每 5–10 ms 醒一次，记录「应醒时刻 → 实醒时刻」的差。
这是唯一能区分「服务器慢」和「循环被占住」的手段——请求墙钟时间两者都会变长。

判读：

| 现象 | 结论 |
| --- | --- |
| p50 lag < 1 ms，p99 与请求延迟同步上升 | 等待在 Core 或服务端，循环健康 |
| p50 lag 本身就在涨 | 循环被同步回调占住，查同步前缀 |
| lag 峰值 ≈ 请求数 × 单请求前缀 | 就是 H1 |

### 2.2 并发洪峰阶梯

`tools/async-diag.py flood --ladder 64,256,1024`：

> **这张表已作废，不要引用。** 2026-11 复测时发现：它记录的数字与它声称测的形状
> 不一致——表头说的是 `flood`，而当前 `tools/async-diag.py:150` 的 `flood` 给每个
> 请求一个**不同 URL**（HttpCache 因此不串行化），单 URL 与不同 URL 的 lag 差两个量级。
> 用当前工具重跑得到 QPS 952/958/936、lag_p99 6.8/13.2/7.4 ms，**1178 ms 复现不出来**；
> 用单 URL 自写探针重跑得到 lag_p99 恒为 1.6 ms、峰值 236 ms。
>
> 下表保留为历史记录，仅用于说明「当时测过」，不再作为任何结论的依据：
>
> | `max_clients` | QPS | p99 | lag_p99 | RSS 峰值 | tasks 峰值 |
> | --- | --- | --- | --- | --- | --- |
> | 64 | 622 | 1367 ms | 11.9 ms | 48900 KiB | 1026 |
> | 256 | 685 | 1078 ms | 20.5 ms | 52012 KiB | 1026 |
> | 1024 | 857 | 922 ms | **1178 ms** | 55708 KiB | 1026 |
>
> 重测口径见 `docs/TBD_VERIFICATION.md` 的 T-6。

下表原来的三条解读建立在上面那张已作废的表上，其中第 3 条（lag_p99 1178 ms 是 H1 的
量化）**不成立**：单 URL 时循环是健康的，慢的是 Core 的 HttpCache 排队。
真正会看到循环停摆的是**不同 URL** 的形状。

三件事（仅第 1、2 条仍然成立）：

1. **RSS 不随 `max_clients` 显著变化**（49 → 55 MiB，+13%），说明没有「每个在飞请求
   各缓冲一份 body」。
2. **tasks 峰值恒为 1026**：闸门限制的是在飞数，不是创建数（H4）。
3. ~~**lag_p99 在 1024 时爆到 1178 ms**：这就是 H1 的量化~~ —— **已撤下**。
   复测复现不出这个数（见本节开头的说明与 `docs/TBD_VERIFICATION.md` T-6）。
   要量化 H1，必须用**不同 URL** 的形状测：那种形状下 512 在飞时 lag p99 是 295 ms，
   而同一 URL 时只有 1.9 ms——两者的差距说明前一份表把 HttpCache 排队读成了循环停摆。

> **这个表测的是单 URL。** 同一 URL 的 1024 个请求里，绝大多数时间不在循环上，而在
> HttpCache 的 per-key 队列里（`net/http/http_cache.cc:371-383`：一个 entry 同时只允许
> 一个 transaction 处在 headers 阶段）。2026-11 复测（1 KiB、fixture 开 ``TCP_NODELAY`）：
>
> | 形状 | qps | 网络线程 CPU/请求 | loop lag p50 | loop lag p99 |
> | --- | --- | --- | --- | --- |
> | 512 在飞，同一 URL | 229 | 3.98 ms | 0.4 ms | 1.9 ms |
> | 512 在飞，512 个不同 URL | 1212 | 0.68 ms | 0.8 ms | 295 ms |
>
> 两列读法相反：**同一 URL 时循环很健康**（lag p99 1.9 ms），慢的是 Core 的排队；
> **不同 URL 时循环停摆 295 ms**，那才是 H1 本身。前一份表把两者混在一起，读数会被
> 引向「循环被占住」这个错误归因。

### 2.3 慢消费者

`tools/async-diag.py slow-consumer --chunks 256 --consumer-ms 2`：判据是**斜率归零**，
不是绝对增长（见 H3）。

---

## 3. 诊断手段

| 生态对应物 | 本库 | 命令 |
| --- | --- | --- |
| Node `perf_hooks.monitorEventLoopDelay` | lag 监控协程 | `async-diag.py lag` |
| `process._getActiveHandles()` | 未完成 task 计数 | `async-diag.py futures` / `tasks` |
| async_hooks / TraceId | 无内置追踪，用 `cProfile` + 栈采样 | `prof-attr.py pyhot` / `stacks` |
| 堆快照对比 | RSS 采样 + `tracemalloc` | `async-diag.py slow-consumer` |
| 队列深度 | 服务端在飞峰值 + 库内 `buffered` | `async-diag.py flood` |

**关于 TraceId**：这个库不产生请求级追踪 ID，ABI 也不携带。要贯穿调用链只能在
调用方自己包一层（`contextvars` + 日志），库侧无法提供。这是如实声明，不是遗漏。

一个必须注意的**测量陷阱**：lag 监控本身要能跑起来。第一版把监控协程和 `gather`
放在同一个任务里启动，结果监控自己也被前缀挡住，读到的峰值就是停摆本身——数值对了，
但归因错了。正确做法是**先启动监控、等它产生样本、再发洪峰**。

---

## 4. 优化方向

| 方向 | 实测依据 | 建议 |
| --- | --- | --- |
| **分批提交**（有界提交队列） | 512 请求：一次 gather 停摆 308 ms；每批 64 → 59 ms；每批 16 → 13 ms | 用 `asyncio.Queue(maxsize=N)` 或分批 `gather`，把「创建协程」也纳入背压 |
| **背压与限流** | `max_clients` 只覆盖到响应头（`test_max_clients_gate.py`）；`STREAM_BUFFER_LIMIT` 1 MiB 生效 | 需要端到端限流时用 `max_clients` + 分批；流式消费者不要停在循环里等 |
| **超时与取消传播** | task 不泄漏；socket 由 TCP 半关闭持有 | 一定要设 `timeout`；取消后若对端迟迟不应答，fd 会滞留，需在调用方限流 |
| **回调拆分到独立调度器** | 前缀 73% 在 `prepare_request` | 目前无「把前缀挪到线程池」的接口；可行的做法是调用方用 `asyncio.to_thread` 包整个 `session.get`（会牺牲连接复用收益，需实测权衡） |
| **批处理与去重** | 本库无批量 API | 批量应在服务端做，或上 HTTP/2 多路复用 |
| **连接复用与多路复用** | 每 host 6 条 HTTP/1.1 是硬顶（同步侧实测） | 需要更高单 host 并发：HTTP/2；多目标：多 host 天然并行 |

### 关于「同步→异步」的边界

异步**不提高并行度上限**（两者共用同一条网络线程），它省的是**线程栈与调度**。
所以：

- 线程数远超「每 host 6 × host 数」时，换异步省资源；
- 但换异步不会让 H1 消失——前缀成本与循环停摆是异步侧独有的新问题；
- 一次性提交海量协程时，异步甚至比同步更糟：同步至少把压力分散在多个线程的阻塞上。

---

## 5. 复现命令

```bash
export LD_LIBRARY_PATH=core/binaries/linux-x86_64
export PYTHONPATH=bindings/python

# 事件循环 lag（固定并发）
python3 tools/async-diag.py lag --concurrency 64 --requests 512

# 并发洪峰阶梯
python3 tools/async-diag.py flood --ladder 64,256,1024 --requests 1024

# 慢消费者 / 背压
python3 tools/async-diag.py slow-consumer --chunks 256 --consumer-ms 2

# 未完成 Future 计数
python3 tools/async-diag.py futures --requests 256 --concurrency 64

# 取消与 fd
python3 tools/async-diag.py tasks --requests 32

# 回归测试（`test_async_model.py` 现有 17 条；本节写作时是 6 条，含变异验证）
python3 -m unittest bindings.python.tests.test_async_model -v
```

---


## 5.5 优化实施记录：合并代理解析的重复计算

异步侧的前缀（每个协程在 `await` 之前跑在循环线程上的同步部分）拆开之后，
最显眼的一项不是业务代码，而是**代理解析被重复计算**。

### 发现

用计数包装 `should_bypass_proxies` 统计每请求调用次数：

```
per request, no session proxy, trust_env=True:
  should_bypass_proxies   3.0 calls
  resolve_proxies         1.0 calls
  get_environ_proxies     1.0 calls
```

三次调用里只有一次是必要的。两处重复：

1. `_effective_proxy` 先自己调一次 `should_bypass_proxies`，再调 `resolve_proxies` ——
   而 `resolve_proxies` 内部又做了一次同样的 NO_PROXY 匹配；
2. `resolve_proxies` 调 `get_environ_proxies`，后者开头**又**做一次同样的匹配。

此外，环境变量的扫描做了两遍：一次在守卫里，一次在取值时。

### 修改

新增 `environ_proxy_map()`：一次扫描同时服务守卫与取值，无代理时返回 `None`
（而不是 `{}`），这样「没配置」与「配置了空值」仍然可区分。`resolve_proxies` 与
`_effective_proxy` 各自只保留必要的那一次 NO_PROXY 匹配。

扫描本身也改成 keys-only 迭代：`os.environ.items()` 要物化所有键值对，
实测 2.1 us 对 1.1 us。

### 效果

| 指标 | 改前 | 改后 | 变化 |
| --- | --- | --- | --- |
| `should_bypass_proxies` 每请求调用 | 3 | **1** | -67% |
| `_effective_proxy` | 32.5 us | **1.2 us** | -96% |
| `resolve_proxies`（无代理环境） | 8.2 us | **1.1 us** | -87% |
| 环境扫描 | 2.26 us | **0.87 us** | -61% |
| 异步前缀 p50（256 协程） | 684.8 us | **560.0 us** | **-18.2%** |
| 前缀总和（1024 协程） | 1283 ms | **1187 ms** | -7.5% |

端到端 QPS 变化在噪声内（±1%），因为 512 并发下墙钟由网络往返与连接池主导。
收益体现在**循环停摆**上：

| 场景 | 停摆峰值 | 停摆 p50 |
| --- | --- | --- |
| 512 协程一次 gather | 105.3 ms | 1.28 ms |
| 512 协程，每批 64 | **22.8 ms** | 1.65 ms |
| 512 协程，每批 16 | **21.9 ms** | 0.87 ms |

### 一个回退掉的改动

试过给 `_seed_store_from_jar` 加无锁快路径（已播种时直接返回，不拿锁）：

- 单次耗时 7.3 us → **0.07 us**；
- 端到端 A/B（12 轮 × 512 并发）729 → 719 qps，**-1.4%，stdev 68/72** —— 噪声内。

**收益测不出来，却要背上「无锁读 + 在注释里论证为什么安全」的负担**，所以回退了。
`close()` 里为配合它加的锁也一并回退。判断标准：单点微优化只有在端到端能测出来时才保留。

### 防止回归

`ProxyResolutionTests`（在 `test_async_model.py`，11 条）：

| 测试 | 断言 | 变异验证 |
| --- | --- | --- |
| 无代理 → 直连 | `_effective_proxy` 返回 `None` | — |
| 环境代理生效 | `HTTP_PROXY` → 该值 | — |
| `NO_PROXY` 仍然绕过 | 返回 `None` | — |
| `ALL_PROXY` 覆盖 http | 返回该值 | — |
| 会话 `proxy=` 优先 | 会话值胜出 | — |
| 会话 `proxies=` 生效 | 映射值胜出 | 置空映射 → 值丢失 ✓ |
| `trust_env=False` 忽略环境 | 返回 `None` | — |
| 空值不算配置 | `environ_proxy_map()` 返回 `None` | 去掉空值过滤 → 产生空映射 ✓ |
| 每请求只扫一次环境 | 计数 == 1 | — |
| 每请求只匹配一次 NO_PROXY | 计数 == 1 | 恢复重复调用 → 2 次 ✓ |
| 无代理时不解析 URL | `urlparse` 计数 == 0 | 去掉快速出口 → 2 次 ✓ |

写这些测试时踩了两个坑，都记在测试里：

1. **patch 错了模块**。`sessions.py` 是 `from .utils import should_bypass_proxies`，
   持有自己的引用；只 patch `utils` 的版本时断言恒过。两个模块都要 patch。
2. **断言放错了层级**。「空值不算配置」如果只断言 `_effective_proxy` 返回 `None`，
   把空值过滤删掉也仍然通过（空字符串在下游是 falsy），必须直接断言 `environ_proxy_map()`。

---

## 5.6 同步/异步共同的两处 cookie 路径优化

压测中把 `_absorb_cookies`（每响应都跑）单独计时，发现两处纯浪费。

### 问题 1：没有 Set-Cookie 的响应也拿锁

`_absorb_cookies` 无条件进入 `with self._cookie_lock`，然后才去看有没有
`Set-Cookie`。绝大多数响应没有 cookie，于是每次请求都为「不会发生的事」付一次
会话级锁：单线程 7.4 us，多线程下还会把并发请求串行化。

改成**先扫描再拿锁**：

```python
pending = [(url, headers.get_list("set-cookie"))
           for url, headers in response.history_headers]
pending = [(url, lines) for url, lines in pending if lines]
final_lines = response.headers.get_list("set-cookie")
if not pending and not final_lines:
    return                      # 没有 cookie，不进锁
```

实测：`_absorb_cookies`（无 Set-Cookie）7.4 us → **0.32 us**。
多线程 8 并发吞吐 -3.4%（扫描快于锁），16 并发 -1.1%。

### 问题 2：`absorb_set_cookie` 构造了没人读的第二份 jar

`absorb_set_cookie` 把同一批 `Set-Cookie` 行**解析两遍**：一遍进目标 jar（写入
store 的副作用），一遍进一个临时 jar 作为返回值。`_absorb_cookies` 的调用点从不读
那个返回值——`response.cookies` 由它自己那次调用填充。

给它加 `mirror=True` 参数，`_absorb_cookies` 的四个调用点传 `False`：

```python
def absorb_set_cookie(self, url, lines, mirror=True):
    ...
    self.extract_cookies(response, request)
    if not mirror:
        return received          # 只要写入副作用，不要那份副本
    received.extract_cookies(response, request)
```

公开方法的默认值仍是 `True`——它的文档契约就是返回那个 jar，
`test_session_defects.py` 与 `test_compat.py` 都在用这个返回值。

实测：会话 jar 侧 270–500 us → 与普通 jar 同量级；单请求整体在噪声内（-2.0%，
stdev 143/160），因为 cookie 解析本身仍是 `cookielib` 的成本。

### 防回归

`test_cookie_request_cost.py` 新增 2 条（共 10 条）：

| 测试 | 断言 | 变异验证 |
| --- | --- | --- |
| `test_a_response_without_set_cookie_takes_no_lock` | 锁未被获取 | 无条件加锁 → `1 != 0` ✓ |
| `test_the_session_jar_skips_the_mirror_copy` | store 仍收到 cookie，`response.cookies` 仍报告 | — |

第二条还顺带固定了一个容易搞错的事实：`mirror=False` 对**响应 jar 和会话 jar 都安全**，
因为两者都是「解析进自己」，被跳过的那份从来只是副本。变异验证：把响应 jar 也改成
`mirror=True`，`response.cookies` 输出不变——说明这一侧的跳过是行为中性的。

---

## 6. 结论

1. **事件循环会被同步前缀占住**，这是本次最重要的发现：512 请求一次 `gather` 会让
   循环停摆 145–308 ms，前缀总和 633 ms，其中 **73% 在 `prepare_request`**。
   缓解手段是分批提交（64 → 59 ms，16 → 13 ms），不是调 `max_clients`。
2. **背压成立**：`STREAM_BUFFER_LIMIT` 1 MiB 让慢消费者的 RSS 斜率归零（16 MiB 处走平）。
3. **错误处理与超时不缺**：6 类故障全部终止，批次结束后 pending task +0。
4. **任务队列无界**：`max_clients` 管在飞不管创建；`gather(1024)` 就是 1026 个 task。
5. **取消不泄漏任务**，但**对端不关闭时 socket 会停在半关闭状态**（FIN-WAIT-2 /
   CLOSE-WAIT），对端应答后释放。这不是泄漏，但取消速度持续超过对端回收速度时会
   变成真实的 fd 压力。
6. **异步不提高并行度上限**，它省线程；一次性海量提交时反而引入同步侧没有的循环停摆。

---

## 7. 还没消除的结构性开销

前缀剩下的 560 us 里，`engines.get` 26 us、`prepare_request` 50 us 是确定性成本，
**其余约 480 us 是 `start_async` 与事件循环调度本身**。再往下只有两条路：

1. **减少每请求的跨线程唤醒**（网络线程 → 循环线程）。已作为 ABI 提案登记在
   `docs/NEXT_STEPS.md`「提案：两项性能 ABI 改动」§三（`mn_engine_set_wakeup_batch`），
   预期把唤醒从约 1.7 次/请求降到接近 1 次/批。**本节写于提案阶段**：批量唤醒已落地为 ABI 16，
   Core 侧实现见 `core/source/engine.cc:541-590`，绑定侧见 `sessions.py:5043-5052`；
2. **把同步前缀挪出循环线程**（`asyncio.to_thread`），会牺牲连接复用，需单独评估。

第 1 条的前置条件是同步侧的「合并首包等待」提案（同文件 §二）先落地并观测稳定，
因为两者都改 Core 的事件投递路径。
