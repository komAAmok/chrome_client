# chrome_client 运行时并发 / 稳定性 / 指纹 / 速度 / 泄漏 审查

> 范围：Python facade（`sessions.py`/`engine.py`）→ Rust 绑定（`bindings/python/src/lib.rs`、
> `crates/minicronet`）→ C++ Core（`core/source/engine.cc` 等）。
> 结论基于代码阅读 + **fork 压测（`tools/fork-regression.py`，2026-09-18 已跑）**。
> 标 ⚠️ 的项是风险；P0 已用脚本实测坐实，并已由 C++ 侧 `pthread_atfork` 补丁把
> 「永久死锁」降级为「快速失败」——补丁前/后的对照结果见第三节。
> 补丁已在 **8 个平台全部增编并验证**（2026-09-18），落地明细见第三节表格与
> `docs/FORK_SAFETY_PATCH.md`。

---

## 一、架构总览

```
Python Session ──EngineCache(Rlock)──▶ EngineSlot(PyEngine)
                                            │
                                      Rust Arc<EngineInner>
                                            │  mn_engine_release (引用计数)
                                      C++ mn_engine (URLRequestContext)
                                            │  GetRuntime().task_runner()
                                      Runtime 全局单例
                                        ├─ ThreadPoolInstance (全局线程池)
                                        └─ network_thread_ "MiniCronetNet" (单 IO 线程)
```

关键事实：
- **每个 Engine 一个 Chromium `URLRequestContext`**：自己的 socket 池、TLS session cache、
  H2/H3 池、DNS cache、HTTP cache、cookie store。
- **Runtime 是进程级单例**：`ThreadPoolInstance`（全局线程池）和 `network_thread_`
  （唯一的网络 IO 线程）在进程内共享，所有 Engine 都跑在它上面。
- **Rust 侧 Engine = `Arc<EngineInner>`**，`EngineInner` 持有裸指针，Drop 时调用
  `mn_engine_release`（C++ 引用计数归零才 `delete`）。

---

## 二、三个并发模型

### 1. 线程池（多线程）—— 总体健康

| 检查点 | 结论 |
| --- | --- |
| GIL 释放 | ✅ 所有阻塞网络调用都走 `py.detach(...)`（3.7+）/`py.allow_threads`（3.6）——`start_stream`/`await_response`/`read_body`/`next_body`/`wait_redirect`/`wait_manual`/`upload_*` 全部如此。多线程能真正并行，不会因 GIL 串行。 |
| EngineCache 线程安全 | ✅ `threading.RLock` 包住 `get/discard/clear/close`；两个 worker 抢建同一个 override 只会建一个（同一把锁内幂等）。 |
| Engine 跨线程共享 | ✅ `Arc<EngineInner>` + `unsafe impl Send + Sync`，注释依据是 Core ABI 的线程安全契约（retain/release/engine 操作线程安全）。 |
| ⚠️ Cookie mirror | `EngineSlot.mirror`（`RequestsCookieJar`）是普通 Python dict 子类，**非线程安全**。多线程并发 `_resolve_cookies` 只会让 Python 侧 cookie 副本丢更新，**不影响网络层**（Chromium 的 cookie store 在线程内），属次要竞态。 |

### 2. 协程（asyncio）—— 设计谨慎，清理完整

| 检查点 | 结论 |
| --- | --- |
| 限流 | ✅ `AsyncSession(max_clients=N)` → `asyncio.Semaphore`，`finally` 里 release。 |
| 回调桥 | ✅ Core 事件回调只做 `loop.call_soon_threadsafe(notify)`，**不跑用户代码、不等 I/O**；`notify_scheduled` 原子标志去重。 |
| 循环引用 | ⚠️→✅ 桥接回调持有 event loop + `notify`，`notify` 又持有 `PyRequest`，形成**跨 Rust 的引用环，Python GC 无法打破**——但 `PyRequest::finish()` 在每个终态清掉回调并 `take()` future，打破环。这是文件头注释明说的关键设计。 |
| 异常路径 | ✅ `CancelledError` 和 `BaseException` 都 `state.finish(cancel=True)`；`start_async` 失败也 finish。 |
| WebSocket | ✅ `close/cancel/poll_event(poll_events)` 在 closed/error 终态 `clear_event_callback`。 |

### 3. 进程池（fork）—— ⚠️ 主要风险区

**原先无 `atfork` 钩子**（Python/Rust 两侧至今没有 `register_at_fork`/`pthread_atfork`），
C++ Core 侧已补上（见 `docs/FORK_SAFETY_PATCH.md`）。Python 侧只有两处补救：

- `shared_session()` 有 pid check（子进程自建 session）；
- `EngineCache._reset_after_fork_locked()` 在 `get()` 时检测 pid 变化，**清空 slots 重建**。

风险链（**已由 `tools/fork-regression.py` 实测坐实**）：

1. fork 后，子进程继承了父进程的 **Runtime 单例**——`ThreadPoolInstance` 和
   `network_thread_` 的线程**在 fork 中死亡**，但 `ThreadPoolInstance::Get()` 仍返回非空
   （内存被继承），所以 `Runtime()` 构造函数里的 `if (!Get()) CreateAndStart...` **不会
   重建**，子进程会复用已死的线程池和 IO 线程。
2. `_reset_after_fork_locked` 清空 slots → PyEngine drop → `EngineInner::drop` →
   `mn_engine_release` → 引用计数归零 → `delete engine` → `~Engine()` → `stopped.Wait()`
   永久阻塞。
3. **新建 Session 同样挂起**：`mn_engine_create` → `Engine::Start()` →
   `initialized.Wait()` 也是 PostTask 到已死的 network thread，永久阻塞。

实测结果（watchdog 8s，SIGALRM 杀死挂起子进程；2026-09-18 在 linux-x86_64 复测）：

| 场景 | 补丁前（v0.2.4） | 补丁后 |
| --- | --- | --- |
| fork 后复用父 Session 发请求 | ❌ 永久挂起（SIGALRM） | ✅ 快速失败（`exit 1`） |
| fork 后新建 Session 发请求 | ❌ 永久挂起（SIGALRM） | ✅ 快速失败（`InitializationFailed`，`exit 1`） |

> 复测时父进程请求因环境未配置 `chrome_client` 包路径而未成功，脚本按设计降级为
> "engine 仍会被创建，继续测试"；此时**新建路径依然 `exit 1`**，说明快速失败来自
> atfork 守卫本身，而不是请求失败——这比依赖网络可达的版本更干净地证明了补丁生效。

**8 平台落地情况（2026-09-18 全部增量重编完成）**

补丁已编译进全部 8 个平台产物，ABI 导出数改动前后均为 20（无破坏性变化）：

| 平台 | 产物 | 大小 (bytes) | fork 符号证据 |
| --- | --- | --- | --- |
| linux-x86_64 | `libminicronet.so` | 8,759,144 | `U __register_atfork@GLIBC_2.3.2` + 反汇编调用点 |
| linux-arm64 | `libminicronet.so` | 8,503,200 | `U __register_atfork@GLIBC_2.17` |
| linux-x86 | `libminicronet.so` | 8,578,568 | `U __register_atfork@GLIBC_2.3.2` |
| macos-x86_64 | `libminicronet.dylib` | 8,442,400 | `U _pthread_atfork` |
| macos-arm64 | `libminicronet.dylib` | 7,690,768 | `U _pthread_atfork` |
| windows-x86_64 | `minicronet.dll` | 10,188,288 | 无（Windows 无 fork()，符合设计） |
| windows-x86 | `minicronet.dll` | 7,939,584 | 无（符合设计） |
| windows-arm64 | `minicronet.dll` | 8,768,000 | 无（符合设计） |

> Windows 分支为了让 `base::Lock` 通过 `-Werror,-Wexit-time-destructors` 与 clang
> 线程安全分析，改用 `base::NoDestructor<base::Lock>` + `EXCLUSIVE_LOCK_FUNCTION` /
> `UNLOCK_FUNCTION` 注解；详见 `docs/FORK_SAFETY_PATCH.md` §5.4。

> 结论：只要 **fork 之前** import 了 chrome_client 并创建过 engine（Runtime 已初始化），
> 子进程里**任何**网络操作都无法正常工作——不是"复用旧引擎"的问题，而是 Runtime 单例的
> 线程在 fork 中死亡且不会重建。
>
> **补丁后的边界**（`pthread_atfork` 只做状态标记，不重建 Runtime）：
>
> - 子进程里的网络调用**立即抛 `InitializationFailed`**，不再无限阻塞；
> - 之所以不"重建 Runtime"，是因为 `ThreadPoolInstance` 等 Chromium 全局单例**没有
>   `Destroy()`**、`Set()` 还会去 join 已死的 worker —— 在 child handler 里（只能调用
>   async-signal-safe 函数）重建是不可行的。因此设计目标是**把死锁降级为可预期的失败**；
> - **fork 之后才首次使用 chrome_client 的路径完全正常**（Runtime 尚未初始化，子进程
>   自己完成首次初始化）。
>
> 安全姿势：进程池优先用 **`spawn` 启动方式**；必须用 fork 时，子进程应重建整个连接层
> （`os.exec*` 重启，或改用 spawn），而不要指望复用父进程的 Session。

---

## 三、指纹一致性 —— ✅ 设计上保证

- `EngineConfig` 的 `__hash__`/`__eq__` 用 `(impersonate, proxy, verify, ca_pem, user_agent,
  accept_language, proxy_*, http_version, cache, profile_namespace, generation)` 作 key。
- 因此**同一个 profile 永远命中同一个 Engine**，TLS ClientHello/GREASE、ALPN、H2 SETTINGS、
  H3 参数、QUIC 版本全部由该 Engine 的 URLRequestContext 固定下来，**并发下不会串**。
- 越界 profile / 非 Chromium 家族显式抛 `ImpersonateError`，不静默降级。
- 唯一"偏移"来源是 `generation`（清 cookie 时 +1 换引擎），属于预期行为。

## 四、速度 —— ✅ 结构合理

- 连接复用、TLS session 复用、H2/H3 多路复用都在 Engine 内部，长期存活（LRU 上限 8 个
  override 引擎 + 主引擎）。
- GIL 释放让线程池真并行。
- ⚠️ 单 `network_thread_` 是 Chromium 的标准单线程 IO 模型，DNS/连接等重活由
  `ThreadPoolInstance` 分担；**同 engine 的 HTTP/1.1 受 Chromium「每 host 6 连接」限制**
  （`Session` docstring 已写明，H2/H3 才是提并发上限的正解）。

## 五、无泄漏 —— ✅ 主体到位，两个边角

| 资源 | 释放路径 |
| --- | --- |
| Engine | `Arc<EngineInner>` → `mn_engine_release`（C++ refcount）。有在飞请求持 Arc 时引擎**延迟到请求结束**才释放，安全。 |
| 同步请求 | 非 stream：`read_body` 后 `detach_callback`；异常路径也 detach；`wait_manual` 超时/3xx 走 `cancel()`（→ `finish`）。 |
| 流式 body | `_SyncBodyReader.close()` → `detach_callback`；读完/超限/异常都 close；**用户放弃则靠 `__del__` 安全网**（CPython 即时，PyPy/GC 延迟）。 |
| 异步请求 | `finish()` 打破跨 Rust 引用环（见上），`CancelledError`/`BaseException` 兜底。 |
| WebSocket | closed/error 终态清回调；`close()`/`cancel()` 主动清。 |
| ⚠️ fork 后旧引擎 | 见「进程池」——可能是 hang 而非静默泄漏，优先级更高。 |

---

## 六、风险清单（按优先级）

1. **P0 — fork 后任何网络操作都 hang（已实测坐实；C++ 侧已修，降级为可预期失败）**。
   `tools/fork-regression.py` 复现：父进程发请求 → fork → 子进程复用与新建 Session 均
   SIGALRM 挂起。根因是 Runtime 单例线程在 fork 中死亡且不重建。已按下面的方向落地：
   - ✅ C++ 注册 `pthread_atfork`（`core/source/engine.cc`）：准备/父/子三段钩子统一加锁，
     child 分支把 Runtime 标记为「fork 后不可用」并重置单例指针 → 子进程**快速失败**
     而非永久死锁。见 `docs/FORK_SAFETY_PATCH.md`、`docs/fork-safety.patch`。
   - ✅ **8 平台全部增量重编完成**（2026-09-18）：Linux 三平台与 macOS 两平台均可见
     `__register_atfork` / `_pthread_atfork` 依赖；Windows 三平台正确不含 fork 依赖。
     `core/binaries/` 已同步，manifest sha256/size 复核一致，8 平台 ABI 导出数均为 20。
   - ⚠️ 未做「在 child 分支重建 Runtime」：`ThreadPoolInstance` 没有 `Destroy()`，`Set()`
     还会 join 已死的 worker，child handler 又只能调 async-signal-safe 函数——技术上
     不成立。因此补丁的语义是「把死锁降级为可预期失败」。
   - 📌 仍需文档明确：**多进程请用 `multiprocessing` 的 `spawn` 启动方式**；若必须 fork，
     子进程不要复用父进程的 Session。
   - 📌 短期规避（对未升级的 so 仍适用）：任何工作进程在 fork 之后才 import
     chrome_client / 建 Session。
2. **P2 — Cookie mirror 非线程安全**：多线程并发下 Python 侧 cookie 副本可能丢更新。
   给 `_resolve_cookies`/mirror 读写加锁即可（网络层不受影响）。
3. **P2 — 流式 body 弃置依赖 `__del__`**：非 CPython 解释器（PyPy）下回收延迟，可能短暂
   占用 Core 请求。可给 `Response.close()` 提供显式通道并鼓励调用。
4. **信息 — 单 IO 线程 + HTTP/1.1 每 host 6 连接**：Chromium 固有，不算缺陷，但要写进
   性能预期。

---

## 七、建议的验证实验

```python
# 1. 线程池：一个 Session 并发 50 请求，观察吞吐与正确性
# 2. 协程：AsyncSession(max_clients=...) 并发 200 请求，跑完观察内存是否回落到基线
# 3. 指纹：并发混合两种 impersonate，抓包核对每个连接的 ClientHello 是否各自稳定
# 4. fork：父进程建 Session → fork → 子进程复用（预期暴露 P0）
# 5. 泄漏：循环 1000 次请求（含 stream=True 不读完），观察 RSS/线程数/engine 数
```
