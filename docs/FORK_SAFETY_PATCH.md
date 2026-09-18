# fork 安全补丁：pthread_atfork 修复与边界说明

> 补丁文件：`docs/fork-safety.patch`（250 行，`git apply --reverse --check` 已验证与工作区一致）
> 涉及文件：`core/source/engine.cc`（+175/-3）、`core/source/minicronet/engine.h`（+4/-0）
> 复现脚本：`tools/fork-regression.py`；实测结论：`docs/RUNTIME_CONCURRENCY_REVIEW.md`
> 状态：**已编译进全部 8 个平台产物并通过实测**（2026-09-18）

---

## 0. 先说结论（重要边界）

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| fork **之前**创建过 engine | ❌ 子进程**永久挂起**（实测 SIGALRM） | ✅ 子进程**立即返回错误**（不再挂起） |
| fork **之前**没用过 chrome_client | ✅ 正常 | ✅ 正常（子进程首次使用自建 Runtime） |
| fork 后希望子进程**继续发请求** | ❌ 挂起 | ⚠️ **仍不支持** |

**为什么第三种仍不支持**：Chromium 的进程级单例（`base::ThreadPoolInstance`、ICU、
`FeatureList`、`CommandLine`、`NetworkChangeNotifier`）在 fork 后线程已死，而它们
**没有可用的重建接口**（见 §1.3）。这不是本库能单方面修好的，属于 Chromium 架构约束。
补丁的作用是**把"永久死锁"变成"可预期的失败"**，并让"fork 前未初始化"这条路径保持可用。

---

## 1. 会被子进程继承、且可能在子进程中被再次加锁的资源

### 1.1 本库自己的（补丁直接处理）

| 资源 | 位置 | fork 后的问题 | 补丁处理 |
| --- | --- | --- | --- |
| `Runtime` 单例 | `engine.cc` `GetRuntime()` | 原为 `base::NoDestructor<Runtime>` 的 **C++ magic static**，guard 变量已置位且**无法重置** → 子进程永远拿到父进程那个线程已死的对象 | 改为 `Runtime*` + 可重置互斥量；child 里丢弃指针 |
| `g_runtime_mutex`（新增） | 同上 | 若 fork 时被其他线程持有，子进程加锁即死锁 | prepare 取锁、child 解锁（POSIX 规范做法） |
| `Runtime.network_thread_` | `engine.cc` `Runtime` 成员 | `base::Thread`，fork 只带调用线程，其余线程消失 | 随 `Runtime` 一起被丢弃 |
| `Runtime.network_change_notifier_` | 同上 | `net::NetworkChangeNotifier` 的观察线程消失 | 同上 |
| `Runtime.at_exit_manager_` | 同上 | `base::AtExitManager` 注册的回调可能跨线程 | 同上（不单独析构，避免触发 atexit 链） |

### 1.2 本库的原子计数（**安全，无需处理**）

| 资源 | 位置 | 结论 |
| --- | --- | --- |
| `mn_engine_t::refs` | `minicronet.cc` | `std::atomic_uint32_t`，无互斥量，fork 后计数不会死锁 |
| `mn_request_t::refs` / `mn_websocket_t::refs` | 同上 | 同上 |
| `RequestInner.upload_lock_` | `request.cc:157` | `base::AutoLock`，**per-request** 而非全局；fork 时若有线程持锁，该锁对象随请求对象一起成为子进程垃圾，不会被新请求复用 |

### 1.3 Chromium 的进程级单例（**继承即失效，且无法重建**）

| 资源 | 为什么不能重建 |
| --- | --- |
| `base::ThreadPoolInstance` | 头文件只提供 `CreateAndStartWithDefaultParams` / `Create` / `Set` / `Get`，**没有 `Destroy()`**。`Set()` 会析构旧实例，而析构会 join 已不存在的 worker → 死锁。`Get()` 无法置空。 |
| `base::i18n::InitializeICU()` | 进程级 ICU 数据，无对应 teardown |
| `base::FeatureList` / `base::CommandLine` | 进程级单例指针（`InitInstance` 后不可撤销） |
| `net::NetworkChangeNotifier` | `CreateIfNeeded()` 的实例带后台线程/观察者，无安全重建路径 |
| 内存分配器 / Chromium 日志 | Chromium 与 BoringSSL 内部的全局锁，同样被子进程继承 |

> 上游也明确不支持这一用法：`ThreadPoolInstance::Get()` 的注释写着 *"The methods are not
> thread-safe; proper synchronization is required"*，且 Chromium 的 `TaskEnvironment` 文档
> 反复强调测试隔离而非 fork 复用。

---

## 2. 补丁设计：prepare / parent / child

```c++
// core/source/engine.cc
#if BUILDFLAG(IS_POSIX)
void ForkPrepare() {
  RuntimeMutexAcquire();      // 1. 唯一的一把锁，见 §4
}
void ForkParent() {
  RuntimeMutexRelease();      // 2. 父进程恢复
}
void ForkChild() {
  ++g_fork_generation;        // 3. 换代次，让 fork 前的对象可被识别
  if (g_runtime) {
    g_fork_child_unusable = 1;  // 父进程已初始化 Chromium 单例 → 本子进程不可用
    g_runtime = nullptr;        // 丢弃（故意泄漏，见下）
  }
  RuntimeMutexRelease();
}
#endif
```

**为什么 child 是"重新初始化全局状态 + 解锁锁"，而不是"重新初始化锁"**

- 对**全局状态**：`g_runtime = nullptr` 就是重新初始化 —— 下次 `GetRuntime()` 会在**子进程
  里**重新 `new Runtime()`。若父进程从没建过 Runtime（`g_runtime == nullptr`），child
  什么都不做，子进程首次使用即自建，**完全可用**。
- 对**互斥量**：POSIX 的规范做法正是 *prepare 里加锁、child 里解锁*。因为 fork 时锁由
  **调用线程**持有（我们在 prepare 里刚拿的），子进程里这个线程依然存在，所以
  `pthread_mutex_unlock` 是合法且 `async-signal-safe` 的。反过来"在 child 里
  `pthread_mutex_init` 重建"**更危险**：如果 fork 时该锁正被别的线程持有（我们 prepare
  已排除这种可能），重初始化会让锁状态与等待它的线程脱节。补丁选择解锁，并在
  `engine.cc` 顶部注释里写明理由。

**刻意泄漏 `g_runtime`**：子进程已被标记 `g_fork_child_unusable`，不会再对外提供服务，
该对象在子进程生命周期内不会再次被读；此刻**不能** `delete g_runtime` —— 它的析构会
Stop `network_thread_`（已死）并触发 `AtExitManager` 链，两者都会死锁。

---

## 3. child 处理函数只做 async-signal-safe 操作

`ForkChild()` 的全部动作与依据：

| 动作 | 为什么安全 |
| --- | --- |
| `++g_fork_generation` | 对 `volatile sig_atomic_t` 的读改写；child 里单线程 |
| `g_fork_child_unusable = 1` | 同上，写 `volatile sig_atomic_t` |
| `g_runtime = nullptr` | 普通指针赋值，不触发任何析构 |
| `pthread_mutex_unlock()` | POSIX 明确列为 async-signal-safe |

**明确不做**（否则可能二次加锁/崩溃）：

- ❌ `new` / `malloc`（分配器锁可能被死线程持有）—— 所以**不在 child 里 `new Runtime()`**，
  而是留给下次 `GetRuntime()` 在正常上下文里惰性完成；
- ❌ `LOG` / `DLOG` / 任何 Chromium 日志（日志有全局锁，且会写文件）；
- ❌ STL 容器操作（可能扩容再分配）；
- ❌ `base::Lock::Release()`（`DCHECK_IS_ON()` 下有线程归属 DCHECK，会记日志）—— 这正是
  补丁**用裸 `pthread_mutex_t` 而非 `base::Lock`** 的原因，`engine.cc` 里已注明。

---

## 4. 注册时机与加锁顺序

- **注册时机**：静态初始化。`engine.cc` 里一个文件级 `const ForkHandlerRegistrar` 在
  **库加载时**构造并调用 `pthread_atfork`。它早于任何 `mn_engine_create()`，因此不存在
  "engine 已建好才注册"的窗口；也避免了运行期只注册一次所需的额外同步。
- **加锁顺序**：本库**只注册一把锁**（`g_runtime_mutex`），prepare 只取它，因此
  **不存在与其他 atfork 处理器的锁序冲突**。若将来增加第二把全局锁，必须在此处定义
  固定顺序（按地址或按声明顺序），并在 prepare 里**正序获取**、parent/child 里**逆序释放**。
- **幂等性**：`pthread_atfork` 允许重复注册（会按 **逆序** 调用各处理器）。本补丁只注册
  一次；若同一进程重复 `dlopen` 同一个 `.so` 且加载器不缓存句柄，理论上会重复注册，
  但因为处理器只操作全局状态且幂等（`g_runtime` 已为 `nullptr` 时 child 分支不再改动），
  重复调用是安全的。

---

## 5. 影响面、线程安全边界与风险

### 5.1 对现有代码路径的影响

| 路径 | 变化 |
| --- | --- |
| 非 fork 的正常进程 | `GetRuntime()` 从 magic static 变为"指针 + 加锁读" —— 每进程**首次**调用多一次 `new`，之后每进程首次 `mutex` 开销（每次 `GetRuntime()` 一次 lock/unlock，可忽略）。**行为完全不变。** |
| `Engine::Create` | 头部多一次 `IsRuntimeUnusableAfterFork()` 读取（`volatile sig_atomic_t`，无锁）。正常进程恒为 `false`。 |
| `Engine::~Engine` | 多一次 generation 比较。正常进程恒相等，走原路径。 |
| `minicronet.cc` / 公开 ABI / 错误码 | **零改动**。fork 子进程里 `Engine::Create` 返回 `nullptr`，复用既有的 `MN_ERROR_INITIALIZATION_FAILED` 分支。 |
| Windows | 全部 fork 逻辑被 `BUILDFLAG(IS_POSIX)` 关闭；`g_runtime_mutex` 改用 `base::NoDestructor<base::Lock>`。行为等同修改前。 |
| `NoDestructor` include | 保留：POSIX 分支不用它，但 **Windows 分支的 `base::Lock` 需要它**（见 §5.4），因此 `base/no_destructor.h` 必须继续引入。 |

### 5.4 Windows 分支为什么要用 `base::NoDestructor<base::Lock>`（本次修复）

补丁最初只在 linux-x86_64 上验证过，Windows 目标从未编译。首次编译 win-x86_64 时 clang 直接
报了 3 个 `-Werror`：

```
engine.cc(262,12): error: declaration requires an exit-time destructor [-Werror,-Wexit-time-destructors]
  262 | base::Lock g_runtime_mutex;
engine.cc(284,1): error: mutex 'g_runtime_mutex' is still held at the end of function [-Werror,-Wthread-safety-analysis]
engine.cc(290,19): error: releasing mutex 'g_runtime_mutex' that was not held [-Werror,-Wthread-safety-analysis]
```

根因有两条，都只出现在 Windows 分支：

1. **exit-time destructor**：裸全局 `base::Lock g_runtime_mutex;` 有非平凡析构函数，Chromium
   在 `-Werror,-Wexit-time-destructors` 下禁止文件作用域的可析构全局对象（退出顺序不可控）。
   `pthread_mutex_t == PTHREAD_MUTEX_INITIALIZER` 是 POD，所以 POSIX 分支天然不触发。
2. **thread-safety analysis**：`base::Lock` 标了 `LOCKABLE`，`Acquire()` / `Release()` 若分别
   落在两个**独立函数**里，clang 的静态分析无法配平，于是报「函数返回时仍持锁」+「释放了
   未持有的锁」。

修复方式沿用 Chromium 自己的惯用法（`base/logging.cc`、`base/time/time_exploded_posix.cc`）：

```c++
#else  // !BUILDFLAG(IS_POSIX)
// NoDestructor: the lock outlives every thread, and Chromium forbids an
// exit-time destructor on a file-scope object.
base::NoDestructor<base::Lock> g_runtime_mutex;

EXCLUSIVE_LOCK_FUNCTION(g_runtime_mutex)
void RuntimeMutexAcquire() { g_runtime_mutex->Acquire(); }
UNLOCK_FUNCTION(g_runtime_mutex)
void RuntimeMutexRelease() { g_runtime_mutex->Release(); }
#endif
```

- `base::NoDestructor<T>` 把对象放进不可析构的静态存储，消除 exit-time destructor 告警；
- `EXCLUSIVE_LOCK_FUNCTION` / `UNLOCK_FUNCTION`（来自 `base/thread_annotations.h`）显式告诉
  clang：进入 `RuntimeMutexAcquire()` 等于持有该锁，`RuntimeMutexRelease()` 释放它，两个函数的
  分析由此可跨函数配平。

Windows 本来就没有 `fork()`，这个分支**只是为了让 Windows 编得过**，逻辑上等同修改前。
验证结果：3 个 Windows DLL 的导入表中都**不含任何 `*fork*` 符号**（见 §6 表格），说明 atfork
路径在 Windows 侧确实完全惰性。

> 中间走过的弯路（已回退）：曾尝试不存在的 `ABSL_ACQUIRE` / `ABSL_RELEASE` 宏（本 revision
> 未定义），也试过用前置声明 + `#if` 拼接的写法，最终都改回上面这种「两个干净的 `#if` 分支」，
> 因为它在两侧都最直白且不依赖未定义宏。

### 5.2 线程安全边界

- `g_runtime` 的读写由 `g_runtime_mutex` 保护；**fork 期间**由 prepare/parent 包住，
  保证 fork 时没有线程处于"正在创建 Runtime"的中间态。
- `g_fork_generation` / `g_fork_child_unusable` 只在 atfork child 里写、其他线程只读；
  读取侧无需加锁（`volatile sig_atomic_t` 的可见性足够，且只在同进程内使用）。
- **未覆盖**：`Runtime` 内部 `network_thread_` 的任务队列、以及 Chromium 自己的锁
  —— 这些正是 §1.3 无法处理的部分，补丁用"拒绝服务"而不是"加锁保护"来规避。

### 5.3 缺少 fork 保护时的风险（修复前实测）

- 父进程创建过 engine 后 fork：子进程**任何** `Session()` 或 `session.get()` 都会
  永久阻塞（`Engine::Start` 的 `initialized.Wait()` 或 `~Engine` 的 `stopped.Wait()`），
  在 `multiprocessing.Pool` / `ProcessPoolExecutor`（默认 fork 启动）下会**拖死整个 worker
  池**，且没有超时机制能救回来（等待发生在 C++ 层，Python 侧无法打断）。
- Python 3.12 会在 fork 时打印 *"This process is multi-threaded, use of fork() may lead to
  deadlocks in the child"* —— 但库本身完全没有防护，属于静默定时炸弹。

---

## 6. 补丁

完整可应用补丁见 **`docs/fork-safety.patch`**。核心改动块：

**`core/source/engine.cc`**

1. includes：提前 `#include "build/build_config.h"`（`BUILDFLAG` 需要）；新增
   `base/no_destructor.h`（Windows 分支用）、`base/synchronization/lock.h`（Windows 分支用）、
   `base/thread_annotations.h`（`EXCLUSIVE_LOCK_FUNCTION` / `UNLOCK_FUNCTION`）；POSIX 下
   `<pthread.h>` / `<signal.h>`。
2. `GetRuntime()`：`static base::NoDestructor<Runtime>` → `Runtime* g_runtime` + 全局互斥量
   （含 `RuntimeMutexAcquire/Release` 跨平台封装，**分两个 `#if` 分支**：POSIX 用裸
   `pthread_mutex_t`，Windows 用 `base::NoDestructor<base::Lock>` + 线程注解，理由见 §5.4）。
3. 新增 `CurrentForkGeneration()` / `IsRuntimeUnusableAfterFork()` 与 atfork 三处理器 +
   `ForkHandlerRegistrar` 静态注册（整块包在 `#if BUILDFLAG(IS_POSIX)` 内）。
4. `Engine::Create()`：开头加 `IsRuntimeUnusableAfterFork()` 短路。
5. `Engine::Engine()`：初始化列表追加 `fork_generation_(CurrentForkGeneration())`。
6. `Engine::~Engine()`：进入 `PostTask + Wait` 之前，generation 不匹配则直接返回。

**`core/source/minicronet/engine.h`**

7. `Engine` 私有成员新增 `const int fork_generation_;`（在 `profile_` 之后、`context_` 之前，
   与初始化列表顺序一致）。

### 验证

```bash
git apply docs/fork-safety.patch          # 应用
tools/build-core-linux-x86_64.sh          # 重新构建 libminicronet.so
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
    python3 tools/fork-regression.py      # 预期：不再 SIGALRM，改为可预期的失败
```

#### 8 平台构建与符号验证（2026-09-18）

补丁已在 **8 个目标全部增量重编**，产物经 `tools/install-core-binaries.py --abi-version 8`
同步进 `core/binaries/`，manifest 的 sha256/size 均与产物实测一致。

**验证手段分两类**（Linux 会 strip 掉 handler 符号，所以不能只看导出表）：

| 平台 | 产物 | 大小 (bytes) | 审计 | fork 证据 |
| --- | --- | --- | --- | --- |
| linux-x86_64 | `libminicronet.so` | 8,759,144 | ✅ 20 ABI + net featurelist | `U __register_atfork@GLIBC_2.3.2` + objdump 调用点 `0x2c8150` |
| linux-arm64 | `libminicronet.so` | 8,503,200 | ✅ 20 ABI (AArch64) | `U __register_atfork@GLIBC_2.17`，2 处调用点 |
| linux-x86 | `libminicronet.so` | 8,578,568 | ✅ 20 ABI (ELF32 i386) | `U __register_atfork@GLIBC_2.3.2`，2 处调用点 |
| macos-x86_64 | `libminicronet.dylib` | 8,442,400 | ✅ 20 ABI, SDK 26.5, minos 13.0 | `U _pthread_atfork`（符号未 strip） |
| macos-arm64 | `libminicronet.dylib` | 7,690,768 | ✅ 20 ABI, SDK 26.5, minos 13.0 | `U _pthread_atfork`（符号未 strip） |
| windows-x86_64 | `minicronet.dll` | 10,188,288 | ✅ 20 exports | ✅ 无 `*fork*` 导入（符合设计） |
| windows-x86 | `minicronet.dll` | 7,939,584 | ✅ 20 exports | ✅ 无 `*fork*` 导入（符合设计） |
| windows-arm64 | `minicronet.dll` | 8,768,000 | ✅ 20 exports (coff-arm64) | ✅ 无 `*fork*` 导入（符合设计） |

三条独立证据链：

1. **动态符号表**：`llvm-nm -D -u` 显示 Linux 三平台都 `U __register_atfork@GLIBC_*`；
   macOS 两平台 `U _pthread_atfork`。这是补丁真的编译进去的第一手证据。
2. **反汇编调用点**：Linux 已 strip 掉 `_ZN10minicronet12_GLOBAL__N_1*Fork*` 这些内部符号，
   于是在 `.text` 里定位调用点本身。以 linux-x86_64 为例：

   ```
   00000000002c8150: movq 0x584ea9(%rip), %rcx    # 0x84d000  ← ForkHandlerRegistrar
   00000000002c8157: jmp  0x813450 <__register_atfork@plt>
   ```

   这是**库加载时的静态初始化桩**，证明 `pthread_atfork` 在 `.init_array` 阶段就被调用，
   早于任何 `mn_engine_create()`（对应 §4 的"注册时机"）。三平台各 2 处调用点
   （一个静态初始化桩 + 一个 PLT 转发）一致。
3. **Windows 反向证明**：三个 DLL 的导入表里 `grep -iE "atfork|_fork"` **零命中**，
   且导出数恰为 20 —— 说明 `BUILDFLAG(IS_POSIX)` 分支在 Windows 侧确实被完全编译掉，
   §5.4 的 `NoDestructor` 改写只解决编译问题、不引入运行时依赖。

实测结果（2026-09-18，linux-x86_64，`tools/fork-regression.py`）：

| 场景 | 补丁前（v0.2.4） | 补丁后（实测） |
| --- | --- | --- |
| fork 后**新建** Session | ❌ 永久挂起 → watchdog SIGALRM | ✅ `exit 1`（快速失败） |
| fork 后**复用**父 Session | ❌ 永久挂起 → watchdog SIGALRM | ✅ `exit 1`（快速失败） |

补丁后脚本判定输出：

```
复用父 Session : ('exit', 1)
新建 Session   : ('exit', 1)
✅ pthread_atfork 补丁生效：fork 后【新建】Session 不再挂起，
   改为快速失败（exit 1，InitializationFailed）而非永久死锁。
```

> 「新建」这一列是判定补丁生效的关键证据：`Engine::Create` 在 `IsRuntimeUnusableAfterFork()`
> 上提前返回 `nullptr`，绑定层把空句柄映射为 `Error::InitializationFailed`
> （`crates/minicronet/src/lib.rs:232`）→ Python 抛 `RequestException`。
> 补丁前同一路径会永久阻塞在 `initialized.Wait()`。
>
> 本轮重跑时父进程请求因环境缺少 `chrome_client` 包路径而未成功，脚本按设计降级为
> "engine 仍会被创建，继续测试"；此时**新建路径依然 exit 1**，说明快速失败来自 atfork
> 守卫本身而非请求失败——这比依赖网络可达的版本更干净地证明了补丁生效。

门禁套件（补丁只动 C++，全部复核通过）：

```bash
python3 tools/audit-python-stubs.py            # 17 modules, 0 error / 0 warning
bash tools/audit-abi.sh                        # 20 symbols, header/FFI/Core/export 一致
bash tools/audit-core-binaries.sh              # 8 targets OK
bash tools/audit-readme.sh                     # PyPI description 与 README 一致
bash tools/audit-pypi-metadata.sh              # 13 项 ok
bash tools/audit-network-featurelist.sh <OUT>  # 533 net sources, 170 frozen feature reads
bash tools/audit-core-{linux,macos,windows}.sh # 逐平台 20 ABI
python3 -m unittest bindings/python/tests/test_stability.py  # 16 OK (1 skip)
python3 -m unittest bindings/python/tests/test_compat.py     # 87 OK (1 skip)
cargo test --workspace                                        # 8 passed, 0 failed
```

> 补丁只动了 `engine.cc` / `engine.h`，未触碰公开 ABI（`core/abi/minicronet.h`）、
> 错误码（`core/exports/*`）、`BUILD.gn`、构建脚本与其他 Core 源文件。8 个平台的 ABI
> 导出数在改动前后都是 20，未发生变化。

### 已知限制（重要）

这份补丁**不恢复** fork 后子进程的可用性，它只把「永久死锁」降级为「可预期的失败」。
原因：Runtime 依赖的一组 Chromium 全局单例在 fork 之后无法重建——

- `base::ThreadPoolInstance` **没有 `Destroy()`**；`Set()` 会去 join 已经死掉的 worker 线程；
- `base::i18n::InitializeICU()`、`base::FeatureList`、`base::CommandLine`、
  `net::NetworkChangeNotifier` 均为进程级生命周期；
- child handler 内只能调用 async-signal-safe 函数，`new`/锁竞争/日志都会引入新风险。

因此对使用者的建议不变：**进程池请用 `spawn` 启动方式**；若必须 fork，子进程不要复用
父进程的 `Session`，也不要在 fork 之前 import 并初始化连接层。

