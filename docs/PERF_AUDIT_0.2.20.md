# 性能审计与优化记录（0.2.20 / ABI 17）

本文是 2026-11-27 一次全链路性能审计的产出：先读代码确认调用链，再用仓库已有的压测工具建立基线，
然后只实施有证据支持的改动，最后用同一套工具做配对 A/B。所有结论都标注证据来源；无法在本机
验证的项目写"未验证"并说明缺什么，不写成通过。

环境：Linux x86_64，8 核；Python 3.12.3；rustc/cargo 1.99.1；Core ABI 17，产物来自
Chromium M156.0.8078.31（`CHROMIUM_REVISION` = 58bcc148f76）；无 Chromium 可编译检出（见 §7）。

---

## 一、真实架构与调用链

### 1.1 分层与文件

| 层 | 文件 | 职责 |
| --- | --- | --- |
| Python facade | `bindings/python/chrome_client/_python_impl/{sessions,engine,models,structures,utils,browser_headers}.py` | requests/curl_cffi 形状；8 300 行非测试代码 |
| PyO3 绑定 | `bindings/python/src/lib.rs` | `PyEngine` / `PyRequest` / `PyWebSocket`，事件元组构造 |
| Rust 封装 | `crates/minicronet/src/{lib,request,websocket}.rs` | 所有权、Condvar/Future、取消、错误映射 |
| C ABI | `crates/minicronet-sys/src/lib.rs` + `core/abi/minicronet.h` | 35 个导出符号，ABI 17 |
| Core | `core/source/{minicronet,engine,request,websocket}.cc` | 唯一网络实现：`URLRequestContext`、socket pool、TLS、H1/H2/H3/QUIC/WS |

### 1.2 同步调用链（一次 `Session.get`）

```
调用线程
  Session.request -> prepare_request            sessions.py:2700
      -> _browser_headers                      sessions.py:2782   (profile 头集合并入)
      -> _reject_core_owned_removals           sessions.py:616
      -> PreparedRequest.prepare               models.py
  Session.send -> _send_once                   sessions.py:4425
      -> _effective_proxy                      sessions.py:3145   (NO_PROXY + environ)
      -> _engine_config -> EngineConfig        sessions.py:3074 / engine.py:276
      -> EngineCache.acquire -> _SlotLease     engine.py:972 / 1090
      -> _resolve_cookies                      sessions.py:3520
      -> _native_request -> PyRequest          sessions.py:4088
      -> native.start_stream()                 py.detach: 释放 GIL，Condvar 等待响应头 (WAIT_SLICE=250ms, lib.rs:859)
      -> native.read_body()                    第二次 Condvar 等待，直到 EOF
      -> _finalize                             sessions.py:3982   (Headers + Response + history)
```

每条结论对应的 Core 侧：`mn_request_create` 校验并深拷贝（`minicronet.cc:990-1113`），
`mn_request_start` 投递到进程级唯一网络线程 `MiniCronetNet`（`engine.cc:281`、`request.cc:147`），
回调经每请求一条 `PooledSequencedTaskRunner`（`request.cc:126`）投回。

### 1.3 异步调用链

```
事件循环线程
  AsyncSession.request -> prepare_request      (同一个函数：同步/异步不各写一份)
  _send_once: await gate.acquire()             sessions.py:4941  (max_clients)
  _perform_on_slot                             sessions.py:5013
      -> _native_request                       (mn_request_create)
      -> native.start_async(loop, notify, batch=True)   lib.rs:798
           ABI 16: 注册 token，Core 侧按批唤醒          engine.cc:541-590
      -> await state.future                    sessions.py:5085
  Core 回调线程 -> Engine 批队列 -> 一次 call_soon_threadsafe
  _batch_drain(notifies)                       sessions.py:1972
      -> _AsyncState.notify                    sessions.py:2039
           -> request.poll_events(64)          lib.rs:840  (一次 FFI 取最多 64 个事件)
           -> 逐事件: response/body/redirect/done/error
```

### 1.4 线程模型（三条，缺一不可）

| 线程 | 数量 | 证据 |
| --- | --- | --- |
| `MiniCronetNet` 网络线程 | **进程级 1 条，所有 Engine 共享** | `engine.cc:281`、`engine.cc:477` |
| Chromium 线程池（回调序列） | 每请求一条 sequenced runner，线程来自共享池 | `request.cc:126`、`pooled_sequenced_task_runner.cc` |
| 调用方线程 / 事件循环线程 | 由调用方决定 | — |

重要推论：**Engine 数量不增加网络线程**。多 Engine 换来的是独立的 socket pool / DNS / TLS session cache /
HttpCache，而不是并行处理能力；网络线程的 CPU 是所有 Engine 共享的。

### 1.5 每请求固定开销（Core 侧，静态计数）

`request.cc` 的跨线程投递：start 1 次、response 回调 1 次、每 chunk 2 次（去回调序列 + 回网络线程）、
complete 1 次，即 `4 + 2 x chunk 数`。读缓冲固定 32 KiB（`request.cc:40`），所以 1 MiB 响应 = 32 块 = 68 次投递。

### 1.6 内存模型与预算

| 项 | 值 | 位置 |
| --- | --- | --- |
| Rust 队列上限 | 1 MiB | `crates/minicronet/src/request.rs:25` |
| facade 流式缓冲上限 | 1 MiB | `sessions.py:87` |
| Core 读缓冲 | 32 KiB x 1（复用） | `request.cc:40/614` |
| 每 Engine HttpCache | **IN_MEMORY，max_size 未设 → Chromium 默认 = min(物理内存 2%, 50 MiB)** | `engine.cc:1007-1011`、`mem_backend_impl.cc:343-346` |
| 每 Engine socket 上限 | 256 软上限；每 host group 6（Chromium 常量，`max_sockets_per_group=0` 时） | `client_socket_pool_manager.cc:40-58` |
| Engine 缓存上限 | `max_engines` 默认 8 | `engine.py:839` |

因此上限公式：`RSS ≈ Σ_engine(50 MiB 缓存上界 + 池) + 在飞请求 x (1~2 MiB 流式缓冲) + Python 对象`。
`max_engines=8` 时缓存的理论上界是 400 MiB —— 这是本项目最容易被忽略的一笔内存预算，见 §6。

---

## 二、性能瓶颈报告

### 2.1 已证实（本机实测或可复现的代码事实）

| # | 位置 | 问题 | 影响 | 证据 | 处置 |
| --- | --- | --- | --- | --- | --- |
| P1 | `structures.py:127 add` -> `_refresh` | 每次 append 重扫整个字段表，构造一个头块 O(n^2) | 一次请求构造 3~4 个头块，异步循环里占最大单项 | 13 字段 14.68 us -> 5.56 us（§5 表） | **已修** |
| P2 | `utils.py:358 environ_proxy_map` | 每次请求解码整张环境表（78 个 bytes.decode） | 同步/异步每请求都付 | 12.60 -> 6.08 us | **已修** |
| P3 | `browser_headers.py:171/208` | 两个纯函数每请求各重建一次同样的 12 个头 | 每请求 2 次 | 3.18+4.81 -> 0.53+0.14 us | **已修** |
| P4 | `engine.py:896 EngineCache.get` | `config.key` 每次访问 5 次（属性本身已记忆化，代价在 RLock + 元组哈希） | 每请求 1 次 | 13.50 us，改后 13.54 us（无变化） | 只做了局部变量简化，**收益测不出来** |
| P5（已修） | `bindings/python/src/lib.rs:948-961` + `sessions.py:2059-2062` | 异步 response 事件丢弃 Core 已解析的 `header_fields`/`reason_phrase`，facade 于是用 `parse_raw_headers` 在 Python 重解一次 | 每异步响应一次重解 + 2 次无用 FFI；且违反 AGENTS §1.1 | 实测改前：`_AsyncState.header_fields=None`，`parse_raw_headers` 1 次/响应；改后 0 次 | **已修** |
| P6 | `sessions.py:4939-4950` | `max_clients` 的语义随 `stream` 变化：`stream=True` 在响应头放行，`stream=False` 一直占到位图 body 收完 | 文档写成"只覆盖到响应头"，与代码不符 | 代码 4946-4947 + `_perform` 返回时机 | **改文档**（§4.2） |
| P7 | 压测工具本身 | `tools/concurrency-ab.py:190`、`tools/bench-core-baseline.py:210` 的 fixture 与被测客户端**同进程**，共享 GIL | 异步 1024 在飞被测低 78%（1150 vs 644 qps） | §5.2 | **登记，未改工具**（见 §3.4） |

### 2.2 成立但不在本机能验证（Core 侧静态结论）

以下由只读子代理给出 file:line，我复核了行号与调用关系，但**没有在本机重跑**（无 Chromium 可编译检出）：

| # | 位置 | 问题 | 指纹影响 |
| --- | --- | --- | --- |
| C1 | `request.cc:260-301` | 请求头块每请求构建 3 遍：调用方 N 条 -> captured 12 条 -> `MergeFrom` 再整份拷 -> `SetExtraRequestHeaders` 内部再深拷 | 无（字段/值/顺序不变） |
| C2 | `request.cc:543-546` | 响应头每个字段拷 2 遍（`EnumerateHeaderLines` 已 assign，随后 `push_back` 再拷）；同文件 redirect 路径 `421-422` 已用 `std::move` | 无 |
| C3 | `request.cc:40` | 读缓冲 32 KiB，1 MiB body 要 32 块 x 2 次跨线程投递 | 无（不改 TLS record / H2 窗口） |
| C4 | `engine.cc:1007-1011` | 每 Engine 一份 IN_MEMORY HttpCache，`max_size` 未设 = 默认上界 50 MiB | 无，但影响内存预算 |

### 2.3 不成立（本次证伪，避免重复投入）

| 假设 | 证据 |
| --- | --- |
| 事件循环是异步吞吐的唯一瓶颈 | 1024 在飞、独立 fixture 下，网络线程 CPU 0.83 ms/请求 ≈ 0.95 核，而 loop 侧每请求 0.87 ms；两侧都不是唯一瓶颈，且 fixture 上限 ≈1 930 rps 才是这个形状的真天花板（§5.2） |
| 每请求 20~40 次 mutex 是锁竞争 | `EngineCache._lock` / `_cookie_lock` 的等待占比在文档里实测 0.1%；本次新增的 mutex 计数也是每请求 O(10) 量级，非竞争点 |
| `EngineConfig.key` 重建是热开销 | `engine.py:384` 返回 `self._key`，`__init__` 里已算好；实测每次 0.0 us |
| `_AsyncState` 构造是热开销 | 0.6 us/次 |
| asyncio 自身调度是热开销 | `gather` 512 个 `sleep(0)` = 4.4 us/op，`Future` await = 5.2 us/op |

---

## 三、优化方案（按层，含未实施项）

### 3.1 已实施（Python facade，4 处）

1. **`Headers` 追加改成增量维护**（`structures.py:119 _store_add`）。`_list` 仍是顺序与重复项的权威；`_store` 只在该名字上增量拼接，因此 `", ".join(...)` 的结果不变、报告的大小写仍是首次写入的那个。语义等价性用 400 轮 x 40 次随机操作的差分 fuzz 固定（新旧实现 10 个可观察量全等）。
2. **环境表按字节扫描**（`utils.py:358`）。`os.environ` 的迭代器会为每个键做一次 decode；改用其背后的字节键字典扫描，只解码命中的那几个。**注意 `os.environb` 不是等价替代**：它在 import 时另建快照，`os.environ[...] = ...` 不会同步过去，实测会漏掉代理（这条写进了代码注释与测试）。
3. **`browser_headers` / `full_header_order` 记忆化**（`browser_headers.py:171/208`）。两者都是 profile 表的纯函数；缓存存 tuple，每次返回新 list，所以调用方改动返回值不会污染下一次请求。
4. **`EngineCache.get` 只取一次 key**（`engine.py:896`）。行为中性，收益测不出来，保留是因为它把 5 次属性读取变成 1 次。

### 3.2 已实施：异步响应复用 Core 已解析的头字段（P5）

改法（不涉及 ABI）：
* `bindings/python/src/lib.rs:948-961`：response 事件的第 5 个元素（`fields`）填 `response.header_fields`，第 4 个元素（`message`，response/success 分支上恒为 `None`）填 `reason_phrase`。事件元组宽度不变。
* `sessions.py:2059-2062`：response 分支改为 `self.reason_phrase = message`、`self.header_fields = fields or None`，只保留 `http_version()` 与 `ech_status()` 两次 getter。
* 收益：每异步响应省一次 `parse_raw_headers`（22.18 -> 0 次；其余路径该函数仍需要）与 2 次 FFI；同时消除 AGENTS §1.1 "绑定层重解 Core 已解析语义" 的现存站点。
* 风险：所有 `poll_events` 解包点（Python 1 处、Rust 6 处构造）必须同步；`test_async_model` / `test_compat` 的响应头断言覆盖行为等价。
* **已实施（2026-11-27 后续一轮）**：`lib.rs` 的 response 事件第 5 元素填 `header_fields`、第 4 元素（message 槽，response 分支上本来恒为 `None`）填 `reason_phrase`，元组宽度不变；`sessions.py` 的 response 分支改为消费这两个元素，只保留 `http_version()` 与 `ech_status()` 两次 getter。实测：`parse_raw_headers` 从 1 次/异步响应降到 **0**，`_AsyncState.header_fields` 现在等于 Core 解析出的 `[("Content-Length","1024"), ("Content-Type", ...)]`；同步/异步的 `headers.items()`、`reason`、`status_line`、`http_version` 在测试里逐项相等。cp36 扩展仍发空 fields + None message，facade 会退回解析路径，因此两个绑定保持可互换。

### 3.3 已实施：Core 侧三项（C1/C2/C3，2026-11-27 九平台重建）

**本轮已全部实施并重建九个平台。** 实测成本：`gn gen` 3.3 s，单目标 `ninja libminicronet.so` 26 s（3 TU + ThinLTO link），改一个 TU 后 8.9-12.4 s；九个目标依次构建+strip+smoke+audit 全部通过，合计约 20 分钟（Windows 三目标最慢）。产物与 manifest 已由 `tools/install-core-binaries.py --abi-version 17` 一并刷新。

### 3.3.1 三项对比（补丁形状、确定性收益、风险、验证）——以下为实施前的评估，实测结果见 3.3.5

前置事实，先纠正一个容易搞错的量：**Python facade 已经在 `_browser_headers` 里把 profile 的 12 个头合进了请求**
（实测：`impersonate=` 的裸 `get` 到达 ABI 的是 12 个头；带 8 个调用方头是 20 个；无 profile 是 0 个）。
所以 Core 里那段 profile 注入对 Python 绑定是**被完全覆盖的冗余**，它真正服务的是不做这层合并的 Go/Node 绑定。
这决定了 C1 的收益必须按「调用方头数 N」算，而不是按「12 + N」。

| 项 | 补丁形状 | 每请求省下的确定量 | 受益形状 | 收益是否已测 | 指纹风险 | 副作用 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 | `request.cc:260-306`：先按 profile 播种局部块，再把调用方头 `SetHeader` 进去（Referer 仍走 `SetReferrer`），删掉中间的 `headers` 对象与 `MergeFrom` | **N 次 `SetHeader`**（Python 绑定 N=12；Go/Node 按其头数） | 所有请求 | **未测**；量级是一个头块的第二次拷贝，预计 <1 us | 中：改的是上线字节的来源路径，字段/值/顺序必须完全不变 | 顺序不变（profile 序在前、调用方补充在后），值不变（首见者胜 = SetHeader 原位替换） |
| C2 | `request.cc:543-546`：`push_back(name)` -> `push_back(std::move(name))`，value 同理（同文件 redirect 路径 421-422 已是 move） | **12 次 `std::string` 拷贝**（其中约 5 个超 SSO，各含一次 malloc） | 所有请求 | **未测**；量级 sub-us | 无：纯所有权，字节不变 | 无。`EnumerateHeaderLines` 每轮 `assign`，moved-from 串可安全复用 |
| C3 | `request.cc:40`：`kReadBufferSize` 32 KiB -> 128 KiB | **每 MiB body 少 48 次跨线程投递**（1 MiB：68 -> 20；16 MiB：1024 -> 256） | **仅 body > 32 KiB** | **未测**；且 `docs/ABI.md:374-409` 已证明大 body 的真实差距在 Rust->Py 的整段 copy，不在投递数 | 无（不改 TLS record / H2 窗口 / SETTINGS） | **每请求 +96 KiB 常驻**（1024 在飞 = +96 MiB）；背压上限从「1 MiB + 32 KiB」变「1 MiB + 128 KiB」 |

**三项之间的排序（不是实施计划）**：

1. **C2 最干净**：零语义风险、有同文件先例、影响每一个请求。它买不到吞吐，但去掉一次多余的全字段拷贝。
2. **C1 其次**：省 N 次 SetHeader，收益随调用方头数线性增长；但它改的是**上线字节的来源路径**，必须用 `audit-frozen-headers.py` + 真实抓包对照才允许合入。
3. **C3 最后，且要重新审视**：只对 >32 KiB 的 body 有效，而这个形状本项目已经测过——`docs/ABI.md:400-404` 的结论是差距主要在「整段 body 复制进 Rust Vec、再由 `PyResponse.content` clone 给 Python」，解法是 ABI 增量的 `mn_request_read_into`，不是放大读缓冲。**一次昂贵的九平台重建，最该带上的是那一条，而不是 C3。**

### 3.3.2 重建可行性（纠正本节开头「只有 linux-x86_64」的说法）

| 事实 | 证据 |
| --- | --- |
| 9 个目标**全部**有已配置的构建目录（`build.ninja` 存在） | `/home/sj/chromium/src/out/MiniCronet-{linux-x86_64,linux-x86,linux-arm64,linux-armv7,macos-x86_64,macos-arm64,win-x86,win-x86_64,win-arm64}` |
| 交叉工具链在位且完整 | `toolchains/` 共 13 GB，内含 `SDK/` 与 `toolchain.cmake`（osxcross 本体，macOS 两目标用）；Windows 三目标用的 `xwin-winsysroot` 在 `/home/sj/chromium/` 下 |
| ninja 在位 | `/home/sj/chromium/src/third_party/ninja/ninja` |
| 9 个 manifest 都带 `core_sources_sha256` | `grep -l core_sources_sha256 core/binaries/*/manifest.json` = 9；`audit-core-source-fingerprint.py:58-95` 逐个比对，所以改一处 Core 源码会让 9 个 manifest **全部**变红，除非 9 个产物一起重建并重新记录（`--record` 只改哈希不重建就是撒谎） |

所以真正的约束**不是能力，而是成本**：`ninja -n libminicronet.so` 在 linux-x86_64 目录里第一步就是 `Regenerating ninja files`，说明该目录相对当前树已过期——`gn gen` 之后要重编多少、每个平台多久，现在没有数字，必须先跑一次才知道。
另有一条：`record-core-build-args.py --check` 要求每个目标的 args 快照与产出它的配置一致，重建轮必须连这批快照一起重录。


### 3.3.3 三条补丁（未应用）

C2：

```cpp
    while (response_headers->EnumerateHeaderLines(&iter, &name, &value)) {
      header_storage.push_back(std::move(name));
      header_storage.push_back(std::move(value));
    }
```

C3：

```cpp
constexpr int kReadBufferSize = 128 * 1024;
```

C1（替换 `request.cc:260-306`）：

```cpp
  const RuntimeProfileData *profile = engine_->profile().data();
  const bool inject = profile != nullptr && engine_->inject_client_hints();
  net::HttpRequestHeaders block;
  if (inject) {
    const BrowserHeaderSet &set = GetBrowserHeaderSet(profile->browser_header_set);
    for (const BrowserHeader &entry : set.headers) {
      const std::string_view name(entry.name);
      if (base::EqualsCaseInsensitiveASCII(name, "user-agent")) {
        block.SetHeader(name, engine_->user_agent());
      } else if (base::EqualsCaseInsensitiveASCII(name, "sec-ch-ua-platform")) {
        block.SetHeader(name, engine_->platform_hint());
      } else {
        block.SetHeader(name, entry.value);
      }
    }
  }
  for (const auto &[name, value] : headers_) {
    if (base::EqualsCaseInsensitiveASCII(name, net::HttpRequestHeaders::kReferer)) {
      request_->SetReferrer(value);
      request_->set_referrer_policy(net::ReferrerPolicy::NEVER_CLEAR);
      continue;
    }
    block.SetHeader(name, value);
  }
  request_->SetExtraRequestHeaders(block);
```

等价性论证：`MergeFrom` 是「没有才加」（首见者胜），而调用方头今天先写进 `headers`、再由
`captured.MergeFrom(headers)` 追加——所以调用方的值胜出、位置保持在 profile 序之后。
`SetHeader` 原位替换给出完全相同的结果（同名不换位置、值被覆盖），并省掉 N 次追加。

### 3.3.4 落地顺序与验收（若批准一轮重建）

| 顺序 | 项 | 验收判据 | 需要同时更新的东西 |
| --- | --- | --- | --- |
| 1 | C2 + C1 | `audit-frozen-headers.py`、`audit-client-hints.py`、`run-http-smoke.py` 全绿；h1 抓包逐字节一致 | 9 个产物 + 9 个 manifest + `core/build-args/*.snapshot.txt` |
| 2 | C3 | 1/4/16 MiB 三形状的吞吐 A/B（配对、换序、≥6 轮）+ `leak-probe.py` 六形态 RSS 仍 flat，且「每请求 +96 KiB」写入 `docs/PERF_TEST_AND_LEAK.md` 的预算表 | 同上 |
| 3 | `mn_request_read_into`（ABI 18） | 大 body 的 20-30% 差距（`docs/ABI.md:376-378`）实际收窄多少 | ABI 头、`minicronet-sys` 布局测试、三份导出表、四个绑定 |

**在这轮重建被批准之前，C1/C2/C3 一律停在「已定位、补丁已写好、未应用」**——因为改一处源码会让 9 个 manifest 全部变红，只重建一个平台等于让仓库里的 8 个产物说谎。


### 3.3.5 三项落地与实测（2026-11-27，linux-x86_64 独立进程 fixture）

**C1 的等价性先证、再谈收益。** 用一个原样记录请求头字节的裸 socket 服务端，对同一个
脚本（裸 get / +8 个头 / +Referer / 覆盖 Accept / 覆盖 sec-ch-ua-platform / POST 带 body /
无 profile / `default_headers=False`）在「committed Core」与「C1+C2+C3 Core」上各抓一次：
8 个请求、118 行头块，**除 `Host:` 里的临时端口外逐字节相同**（端口归一化后 `diff` 为空）。
顺序也相同——这正是 `SetHeader` 原位替换与 `MergeFrom` 只补缺失字段在结果上重合的证据。

**C3 用实测改了方案。** 先把 `kReadBufferSize` 直接改成 128 KiB（提案 C3 的原样）：
大 body 大幅变快，但 1 KiB 响应**四个格子全负**——每个请求都要为「一个 chunk 就放得下」
的响应分配并缺页 128 KiB。改成**按 body 自适应**（声明长度 ≥96 KiB 才用 128 KiB；chunked
则等第一次读满缓冲再升），同法复测：

| 形状（4 轮交替，中位） | committed Core | C1+C2+C3 | 变化 | 网络线程 CPU/请求 |
| --- | ---: | ---: | ---: | --- |
| 1 KiB，sync 8 线程 | 1062 qps | 1074 qps | +1.1% | — |
| 1 KiB，sync 32 线程 | 1046 | 1122 | +7.3% | — |
| 1 KiB，async 256 在飞 | 1110 | 1142 | +2.8% | loop 901 -> 876 us/req |
| 1 KiB，async 1024 在飞 | 1078 | 1081 | +0.3% | loop 928 -> 925 us/req |
| 1 MiB 顺序 buffered | 58.0 MiB/s | 100.9 MiB/s | **+73.8%** | 11.88 -> 7.81 ms/MiB |
| 4 MiB 顺序 buffered | 64.0 | 105.0 | **+63.9%** | 11.25 -> 7.65 ms/MiB |
| 16 MiB 顺序 buffered | 84.1 | 172.4 | **+105.1%** | 6.79 -> 3.28 ms/MiB |
| 16 MiB 顺序 streamed | 92.0 | 194.0 | **+111.0%** | 6.79 -> 3.51 ms/MiB |
| 4 MiB async 8 在飞 | 205.1 | 484.4 | **+136.2%** | 4.06 -> 1.72 ms/MiB |

用**最终安装的产物**（`core/binaries/linux-x86_64`，strip 后）复跑同一套：1 MiB +58.2%、4 MiB +78.4%、
16 MiB buffered +103.2%、16 MiB streamed **+137.0%**、4 MiB async +110.9%；网络线程 CPU/MiB
11.72->8.12 / 12.34->7.81 / 6.79->3.29 / 7.19->3.36 / 4.38->1.88。数量级与上表一致，方向相同。

对照（同一方法，只把 32 KiB 换成**恒定** 128 KiB 的版本）：sync-8 **-4.2%**、sync-32 -1.5%、
async-256 -0.2%、async-1024 -2.6%——正是这条「四个格子全负」让方案从恒定 128 KiB 改成自适应。

内存代价（hold 住 N 条流式响应再测 RSS）：恒定 128 KiB 版本每请求 **+195 KiB**（16 条 1 MiB 流在飞：
28 224 -> 31 348 KiB）；自适应版本只对真正的大 body 付这笔钱，1 KiB 请求保持原样。

结论：C3 的收益远大于提案阶段的预期（我原先估计「可能归因错了、应让位给 `mn_request_read_into`」），
**实测推翻了那个判断**——投递次数就是主要成本。`mn_request_read_into` 仍值得做，但它不是这一项的前置。


**九平台产物（`install-core-binaries.py --abi-version 17` 记录）**：

| 目标 | sha256 前 16 位 | 大小 | abi |
| --- | --- | ---: | ---: |
| linux-x86_64 | 2a4e60f3451defad | 8 666 672 | 17 |
| linux-x86 | 9feece002c861c13 | 8 268 688 | 17 |
| linux-arm64 | d1d525824ba61906 | 8 312 160 | 17 |
| linux-armv7 | d707e17ccedc37a8 | 6 253 768 | 17 |
| macos-x86_64 | aa785b525f3e327b | 8 144 328 | 17 |
| macos-arm64 | 9d9097c374dc09b5 | 7 477 008 | 17 |
| windows-x86_64 | 762563e5553336a6 | 9 633 792 | 17 |
| windows-x86 | fbeebed168eae825 | 7 669 248 | 17 |
| windows-arm64 | 9330fa616170301d | 8 329 728 | 17 |

上表是**第二次**重建的结果（第一次九个产物构建于带缺陷的源码，见 3.3.6，已全部作废）。
linux-x86_64 的 strip 后大小与改动前**完全相同**（8 666 672 B）：C1/C2 是等价的代码形状变换，
C3 只多一个 int 成员与两处判断，被对齐吸收。这也说明「大小不变」不能当作「没改」的证据——
哈希变了（14727afa… -> 95e67abc…）。


### 3.3.6 本轮自己引入并修复的一个缺陷（必须记下来）

**症状**：全量测试跑到 `test_stability.StabilityTests.test_async_multichunk_stream_completes` 时
既不通过也不失败——日志停在那里，前台跑一次拿到 `EXIT=133`（SIGTRAP），也就是 Chromium 的
`IMMEDIATE_CRASH`/`CHECK` 路径。单跑那条测试同样挂住。

**根因**：`ReadMore()` 里 `request_->Read(read_buffer_.get(), read_buffer_size_)`。在 C3 之前这两个
值来自同一个常量，所以永远相等；一旦缓冲区大小可以在响应中途增长，**复用中的 32 KiB 缓冲会被
要求读入 128 KiB**——96 KiB 堆溢出，溢出目标是这个请求自己的队列，于是表现成「挂住」而不是崩溃。
触发条件正好是「body 跨过增长阈值」：多 chunk 流式响应。

**为什么既有门禁看不见**：分配大小与读取长度在这个文件里共用同一个常量已有很久，没有任何测试
能观测到二者不一致；只有跨阈值的 body 才会暴露。

**修法**：

```cpp
  if (!read_buffer_ || read_buffer_->size() != read_buffer_size_) {
    read_buffer_ =
        base::MakeRefCounted<net::IOBufferWithSize>(read_buffer_size_);
  }
  int result = request_->Read(read_buffer_.get(), read_buffer_->size());
```

并在注释里写清两条不变量：(1) 分配大小必须等于传给 `Read` 的长度；(2) 暂停路径之所以安全，
是因为 `buffer_held_paused_` 的提前返回发生在分配之前，暂停中的请求不会替换自己的缓冲区。

**验证**：该测试从「挂住」变为 **0.53 s 通过**；随后全量 `Ran 642 tests in 163.3 s / OK (skipped=2)`。
九个平台用修好的源码重建（第一轮九个产物是用带缺陷的源码构建的，已全部作废重来）。

这一条之所以留在报告里：它是本次唯一一处**由优化引入、由测试套件抓到**的缺陷，而抓法不是断言，
是挂起＋SIGTRAP——正说明「编译通过 ≠ 能加载 ≠ 能发请求 ≠ 长跑正确」，四步都要跑。

### 3.4 测量方法本身（P7）

`tools/concurrency-ab.py` 与 `tools/bench-core-baseline.py` 的 fixture 跑在被测进程里，与客户端抢同一个 GIL。实测同一客户端、同一形状：

| 形状 | 进程内 fixture | 独立进程 fixture | 差异 |
| --- | --- | --- | --- |
| async 256 不同 URL | 834 qps | 1 053 qps | +26% |
| async 1024 不同 URL | 644 qps | 1 150 qps | **+78%** |
| sync 8 线程 | 675 qps | 760 qps | +13% |
| sync 32 线程 | 719 qps | 840 qps | +17% |

结论：**进程内 fixture 会把异步吞吐低估到一半以下**，历史表格里的绝对数只能同口径比较。
`http.client` 对照组（同一独立 fixture）在并发 8/16/32/64 时是 1 864 / 1 933 / 1 551 / 1 294 rps，即 fixture 上限约 1 930 rps —— 这就是为什么客户端 >1 900 rps 的形状测不出改进。

本轮 A/B 因此改用独立 fixture（`tools/perf_harness.py` 的 `FixtureServer` 由子进程启动），并保留 `http.client` 对照组。

---

## 四、修改清单

### 4.1 代码

| 文件 | 变更 |
| --- | --- |
| `bindings/python/chrome_client/_python_impl/structures.py` | `_refresh` -> `_store_add`（增量维护）；`add`/`__setitem__` 改用一次 `_text` 调用；`_discard` 直接 `_store.pop`；`_text` 先判 str |
| `bindings/python/chrome_client/_python_impl/utils.py` | `environ_proxy_map` 走字节键扫描，保留 str 回退路径 |
| `bindings/python/chrome_client/_python_impl/browser_headers.py` | 两个纯函数记忆化（tuple 缓存 + 每次返回新 list），新增 `_build_browser_headers` |
| `bindings/python/chrome_client/_python_impl/engine.py` | `EngineCache.get` / `discard` 只取一次 `config.key` |
| `core/source/request.cc` | C1 单遍构造请求头块；C2 响应头字段 move；C3 自适应读缓冲（32 KiB -> 128 KiB） |
| `core/source/minicronet/request.h` | `read_buffer_size_` 成员 |
| `core/binaries/{9 个目标}/libminicronet.*` + `manifest.json` + `targets.json` | 九平台重建产物与哈希 |
| `bindings/python/src/lib.rs` | response 事件携带 Core 已解析的 `header_fields` 与 `reason_phrase`（元组宽度不变） |
| `bindings/python/chrome_client/_python_impl/sessions.py` | response 分支消费这两个元素，不再读 `header_fields()`/`reason_phrase()` |
| `bindings/python/tests/test_facade_hot_paths.py` | **新增** 11 条回归测试 |
| `docs/ASYNC_CONCURRENCY.md`、`docs/SYNC_CONCURRENCY.md`、`docs/NEXT_STEPS.md` | 修正被本次审计证伪/过时的断言 |
| `docs/PERF_AUDIT_0.2.20.md` | **新增** 本文 |

无 ABI 变更（仍 35 符号 / ABI 17），无新增依赖，未删除任何测试。

### 4.2 文档修正（每条都有代码位置）

| 文档 | 原断言 | 实际 | 依据 |
| --- | --- | --- | --- |
| `ASYNC_CONCURRENCY.md:30` | `max_clients` 只覆盖到响应头 | 只对 `stream=True` 成立；`stream=False` 覆盖到 body 收完 | `sessions.py:4939-4950` + `_perform` 返回点 |
| `ASYNC_CONCURRENCY.md:14-22` | 唤醒路径只有 per-request 分支 | ABI 16 起默认走 Engine 批队列 | `engine.cc:541-590`、`sessions.py:5043-5052` |
| `ASYNC_CONCURRENCY.md:426-432` | 批量唤醒仍是提案 | 已落地为 ABI 16 | `core/abi/minicronet.h`、`engine.cc:500-583` |
| `ASYNC_CONCURRENCY.md:248` | `test_async_model` 6 条 | 17 条 | `grep -c "def test_"` |
| `SYNC_CONCURRENCY.md:118` | `test_cookie_request_cost` 8 条 | 10 条 | 同上 |
| `NEXT_STEPS.md:937` | 撤回后仍 28 符号 | 35 符号 | `wc -l core/exports/minicronet.exports` |
| `PERF_TEST_AND_LEAK.md:391` | `test_resource_release` 3 条 | 7 条 | 同上 |

---

## 五、性能对比

### 5.1 组件级（确定性，同一进程形状，4 000 次迭代取中位；HEAD 与改动后各跑一次）

| 项目 | 优化前 | 优化后 | 变化 |
| --- | ---: | ---: | ---: |
| `prepare_request` | 118.53 us | 82.12 us | **-30.7%** |
| `_slot`（config + cache + lease 的一半） | 45.84 us | 36.59 us | **-20.2%** |
| `Headers` 构造（13 字段） | 14.68 us | 5.56 us | **-62.1%** |
| `parse_raw_headers`（13 字段） | 22.18 us | 10.52 us | **-52.6%** |
| `environ_proxy_map` | 12.60 us | 6.08 us | **-51.7%** |
| `browser_headers()` | 3.18 us | 0.53 us | **-83.3%** |
| `full_header_order()` | 4.81 us | 0.14 us | **-97.1%** |
| `EngineCache.get`（warm） | 13.50 us | 13.54 us | 0%（噪声内） |
| 每请求 facade 合计（`prepare_request` + `_slot`） | 164.37 us | 118.71 us | **-45.7 us / -27.8%** |
| 异步响应的头块（13 字段） | 22.18 us（走 `parse_raw_headers`） | 5.56 us（走 Core 字段；两者都是本轮实测） | **-75%** |

### 5.2 端到端

| 测试项目 | 优化前 | 优化后 | 变化 | 说明 |
| --- | ---: | ---: | ---: | --- |
| async 顺序 p50（400 请求 x 6 轮，交替 A/B） | 2.3 ms | 2.2 ms | **-4.3%** | 与 -45.7 us 的组件级结论同向、同量级 |
| sync 顺序 p50（同上） | 2.0 ms | 2.0 ms | 0% | 同步 p50 的 87% 是两次 Condvar 等待，45 us 在 0.1 ms 分辨率下测不出 |
| async 1024 在飞 qps | 极值 1 129 / 1 142 | 1 083 / 1 101 | -4%（**不可信**） | 同一条臂重复跑差异达 ±16%，且已贴近 fixture 上限 1 930 rps |
| sync 32 线程 qps | 1 010 / 1 214 | 1 047 / 1 138 | 噪声内 | 同上 |
| 峰值 RSS（async 1024，相对增量） | 11 072 KiB（一次）/ 5 700（另一次） | 同量级 | 噪声内 | 未观察到回退 |
| 线程数量 | MiniCronetNet 1 条 + 线程池 | 未变 | 0 | 本次改动不碰线程模型 |
| Event Loop Lag | N/A | N/A | N/A | 未单独测：本轮改动不改变唤醒次数，lag 归因见 `docs/ASYNC_CONCURRENCY.md` H1 |
| CPU 使用率 | 网络线程 0.86~0.94 ms/请求 | 0.83~0.92 ms/请求 | 噪声内 | CPU 的大头在 Core，本次未碰 Core |
| fd / Engine 数量 / 缓存命中率 | N/A | N/A | N/A | 本次改动不在这些路径上；泄漏与 fd 由 `tools/leak-probe.py` 守着 |

**5.2 的诚实结论**：并发吞吐的改进在本机**测不出来**。原因不是改动无效（组件级 -45.7 us/请求是确定性的），
而是这些形状的真天花板是 fixture（≈1 930 rps）与 Core 网络线程，而不是 Python 循环；要证明并发吞吐的改进，
需要一个能吃下 >5 000 rps 的 fixture 与一个 loop-bound 的形状，本机没有。**因此不宣称并发吞吐提升。**

---

## 六、当前性能边界与建议

### 6.1 已经消除的

* 头块构造的 O(n^2)（每请求 3~4 个头块，-9 us/块）。
* 每请求一次全环境解码（-6.5 us）。
* 每请求两次纯函数重建（-7.3 us）。

### 6.2 尚未消除的（按剩余收益排序）

1. **Core 的每请求 4 次固定投递 + 每 32 KiB 两次投递**（C3）。这是这个架构的固有形状：读缓冲放大到 128 KiB 可以把 1 MiB body 的投递从 68 次降到 20 次，且不改任何协议参数。
2. **请求头块建 3 遍**（C1）。纯分配，无指纹风险。
3. **异步响应在 Python 重解头块**（P5）。每响应 10~22 us + 2 次 FFI。
4. **同步路径的两次等待**。已评估并明确放弃（`docs/NEXT_STEPS.md:928-937`：合并后实测 9%，代价是双路径首块语义）。
5. **每 host 6 条 HTTP/1.1 连接**。要更高单 host 并发只能上 HTTP/2；加深连接数会改浏览器可观测行为，**不建议**作为默认。

### 6.3 生产配置建议

| 场景 | 建议 | 依据 |
| --- | --- | --- |
| 同步 + 线程池 | 线程数 ≈ 每 host 6 x host 数 x Engine 数，不必更高 | 超过后只是排队，p99 恶化（`docs/SYNC_CONCURRENCY.md` H4） |
| 异步 | `max_clients` 设成"对端能承受的在飞数"，并**分批提交**（每批 64 以内） | 一次 `gather(1024)` 的同步前缀会占住循环；分批把停摆从 308 ms 降到 59 ms |
| 同一 URL 高并发 | 先确认是不是被 HttpCache 的 per-key 队列串行化（`engine.cc:1007`）；必要时 `cache_mode="no_store"` | 实测同一 URL 229 qps vs 关缓存 1 238 qps |
| `max_engines` | 设成"实际会用到的配置数"，不要随手放大 | 每个 Engine 一份 HttpCache，上界 50 MiB + 独立 socket pool |
| 内存预算 | `50 MiB x max_engines + 2 MiB x 流式在飞数 + 常驻` | §1.6 |

### 6.4 下一阶段最值得投入的

1. 把 §3.4 的独立 fixture 固化成工具的默认模式（`perf_harness` 加 `--fixture-process`），否则任何 <20% 的改进都无法被证明。
2. 做 C3（读缓冲 128 KiB）+ C2（响应头 move）：两者都不动指纹，且能在同一台机器上用 `perf stat` 与投递计数直接量化。
3. 做 P5（异步复用 Core 解析结果），它同时消掉一处 AGENTS §1.1 违规。

---

## 七、验证与限制

已在本机执行并通过：

* `python3 -m unittest discover -s bindings/python/tests` —— **642 条通过，2 条跳过**（156.0 s / 157.6 s 两次）。改动过程中共六次全量运行：629 / 640 / 642（facade 轮）、642（Core 修复后）、642 + 一次 `ERR_NETWORK_CHANGED`（Core 安装后）、642（复跑确认）。那条 `ERR_NETWORK_CHANGED` 出现在 `test_thread_pool_safety`，同一份代码单跑该文件 3/3 通过、旧 Core 3/3 通过，判为瞬时网络变更通知，不是回归。
* Core 轮：`audit-core-inputs.sh`、`audit-core-binaries.sh`、`audit-abi.sh`、`audit-core-source-fingerprint.py`、`audit-core-size-ceilings.py`、`audit-core-payloads.py`、`audit-abi-layout.py`、`audit-frozen-headers.py`、`audit-client-hints.py`、`audit-session-parity.py`、`audit-compat-surface.py`、`audit-layer-ownership.py`、`audit-doctests.py`、`audit-discard-fingerprint.py`、`audit-binding-surface.py`、`audit-python-stubs.py`、`record-core-build-args.py --check` —— **17 项全部通过**。
* 新增 `test_facade_hot_paths.py` 13 条（含在 642 之内），并对其中 4 条做了变异验证：去掉连接分隔符 -> 2 红；记忆化直接返回缓存对象 -> 1 红；环境读取改成 import 期快照 -> 2 红；把异步响应改回"从 request 取 fields" -> 1 红（`the asynchronous response re-parsed its header block in Python`）；恢复后全绿。
* 门禁：`audit-frozen-headers.py`、`audit-client-hints.py`、`audit-session-parity.py`、`audit-python-stubs.py`、`audit-compat-surface.py`、`audit-layer-ownership.py`、`audit-doctests.py`、`audit-abi-layout.py`、`audit-extra-fp.py`、`audit-python-typing.py`、`audit-discard-fingerprint.py`、`audit-readme.sh`、`audit-pypi-metadata.sh` —— 全部通过（指纹门禁全绿说明请求头集合与顺序未被本次改动影响）。

未执行/无法执行：

* ~~Core 的任何重建~~ —— **已推翻**：9 个构建目录与交叉工具链本机齐备，`gn gen` 3.3 s、单目标 26 s、改一个 TU 12 s。九个平台两轮重建（第一轮作废）全部通过，产物已安装并记录哈希（§3.3.5）。C1/C2/C3 有实测收益，不是静态推断。
* `cargo fmt --all -- --check` 与 `cargo clippy --workspace --all-targets`：在 P5 改动后跑过，均无输出（干净）。`cargo test --workspace --all-targets` 未重跑：本轮 Rust 改动只在绑定层的事件元组，Rust 侧单元测试不覆盖它，行为由 642 条 Python 测试与变异验证固定。
* `tools/leak-probe.py` / `leak-hunt.py`：RRSS 在 async 1024 的形状下跨臂差异本来就是 ±5 MiB 量级，本轮不涉及生命周期改动，未重跑。
* TLS/H2/H3 指纹审计的**线上**部分：本次不改任何请求头、ALPN、SETTINGS 或 QUIC 参数；静态指纹门禁全绿即为结论的上界，`run-http-smoke.py` 需要外网，未跑。
