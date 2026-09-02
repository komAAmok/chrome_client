# Python 资源生命周期与背压改造计划

状态：阶段 1--4 已实现初版；阶段 5 尚未实施。

本文记录 `chrome_client` Python 主版本（3.7--3.13）和 Python 3.6 兼容版本的
后续改造顺序。目标是减少回调争用、限制 Python/native 内存增长、明确关闭语义，
同时保持 Python facade 为薄绑定，不在 Python 层复制 Chromium 网络协议。

## 设计边界

- Chromium Core 继续拥有 HTTP、TLS、HTTP/2/3、QUIC、WebSocket 和网络线程。
- 同步路径继续使用 PyO3 的 `detach`（3.7+）/`allow_threads`（3.6）释放 GIL。
- 异步路径继续使用 `asyncio` 事件循环和 `loop.call_soon_threadsafe`，不使用
  Python 线程池。
- 一个请求或 WebSocket 内的事件必须保持顺序；不同请求之间不能因为单个慢消费
  而相互阻塞。
- 所有队列必须有明确的消息数或字节数上限；达到上限时只能暂停、取消并报错，
  不能无限增长或静默丢数据。

## 分阶段顺序

### 1. callback 幂等清理与 Client 关闭

状态：已实现初版（ABI v7）。

目标：所有完成、错误、超时、取消和事件循环关闭路径都能安全解除回调引用。

计划：

- Rust 为 Request/WebSocket 增加幂等的 `detach_callback`/关闭状态转换；清理时
  先原子地标记 inactive，再在不持有状态锁的情况下取出并释放 callback。
- callback 不得在持有 Rust Mutex 时获取 Python GIL，也不得等待正在执行的
  callback；已排队的旧通知通过 inactive 标记丢弃。
- Python 异步请求统一使用 `try/finally` 清理 callback；超时、取消、异常和
  正常完成只能进入一次清理逻辑。
- `Client.close()`/`AsyncClient.aclose()` 变为幂等操作：关闭后拒绝新请求，释放
  Engine 绑定和 callback 引用；已启动的独立 Request 是否取消由显式参数决定。
- 可选的强制路径使用 `close(cancel_pending=True)`，普通 close 不意外取消已启动
  请求。

验收：重复 close 不报错；请求完成后无 Python loop/callback 引用泄漏；取消后关闭
事件循环不会触发回调死锁或 `RuntimeError`。

### 2. HTTP 真流式响应与响应大小限制

状态：已实现初版（ABI v7）。

目标：`stream=True` 不再先聚合完整 body，超大并发响应不会同时复制到多个
`bytearray`。

计划：

- `stream=False` 保持现有 Requests 语义，内部通过流式消费后返回完整 `content`。
- `stream=True` 在收到 response headers 后返回 Response；同步提供
  `iter_content()`/`iter_lines()`，异步提供 `aiter_bytes()`/`aiter_lines()`。
- Rust body 队列按字节数设上限；消费者取走数据后唤醒生产方，生产方永不在 Python
  GIL 或事件循环线程中阻塞。
- 增加可选 `max_response_bytes`；超过上限时取消请求并抛出明确的响应大小异常。
  默认值保持兼容，不改变现有调用行为。
- body chunk 的所有权只在 Rust 队列和当前 Python consumer 之间转移一次，避免
  不必要的二次复制。

当前 ABI v7 初版已经提供上述 facade、`max_response_bytes` 和同步/异步 iterator；
真正的 Core pause/resume 背压仍属于阶段 5。

验收：大响应 `stream=True` 的常驻内存受队列上限控制；取消/异常会释放 body
  iterator；同步迭代等待时释放 GIL；异步迭代不创建线程。

### 3. WebSocket native 有界队列与 Future 唤醒

状态：已实现初版（ABI v7）；队列超限采用取消/错误，尚未具备 Core pause/resume。

目标：移除 Python 层无界 `asyncio.Queue`，让消息背压和生命周期由 Rust/native
统一管理。

计划：

- WebSocket native 事件队列按消息数和总字节数限制；默认上限必须写入 API 文档。
- 异步 `recv()` 等待一个 Future；Core 事件只负责唤醒，`recv()` 被唤醒后直接从
  Rust 队列取消息。
- 一个 WebSocket 同时只允许一个 active `recv()`；并发调用抛出明确异常，避免
  消息顺序和所有权竞争。
- 队列达到上限时，优先使用 Core pause/resume；在 ABI 尚未升级前只能取消连接并
  抛出 buffer-limit 异常，禁止静默丢帧。
- close、cancel、error 和事件循环关闭都唤醒等待中的 recv Future。

验收：生产速度持续高于消费速度时内存有上限；文本、二进制、关闭和异常事件顺序
稳定；取消 recv 后 WebSocket 可安全 close。

### 4. Core 每请求独立顺序回调

状态：已实现源码初版，待各平台 Core 重建验收。

目标：单个慢请求不能阻塞全局 callback runner，同时保持单请求事件顺序。

计划：

- Request/WebSocket 各自创建独立的 `SequencedTaskRunner`；保留 Engine runner
  仅用于兼容旧内部调用。
- 同一请求的 response/body/complete 仍严格保序；不同请求可并行派发。
- callback runner 不得等待 Python consumer；背压必须作用于请求自身，不得阻塞其他
  请求的 callback。
- 该阶段涉及 Core C++ 实现和链接产物，完成后必须重新编译各目标平台 Core；Rust
  FFI 符号不一定变化，但必须重新做 ABI/运行时回归。

验收：一个请求阻塞消费时，其他请求仍可收到 headers/body/complete；32/128/1000
并发矩阵、取消、超时和大响应测试通过。源码改动必须重新编译全部目标平台 Core
后才能完成最终验收。

### 5. ABI v8：HTTP/WebSocket pause/resume 背压

状态：未实施。

目标：实现端到端暂停读取，而不是在 callback runner 或 native 队列中等待。

候选 ABI（名称最终以 ABI 审计为准）：

```c
mn_request_pause_read(mn_request_t *request);
mn_request_resume_read(mn_request_t *request);
mn_websocket_pause_read(mn_websocket_t *websocket);
mn_websocket_resume_read(mn_websocket_t *websocket);
```

实施要求：

- 更新 `core/abi/minicronet.h`、ABI 版本、Rust `minicronet-sys` 和安全封装。
- Core HTTP 使用 pause/resume 控制 `URLRequest::Read`；WebSocket 使用对应的
  接收流控制，不能只在上层丢弃消息。
- 更新导出表、Windows import library、macOS/Linux 导出符号和所有产物 manifest。
- ABI v8 二进制必须拒绝被 ABI v7 绑定误加载；缺少新符号时初始化失败并给出明确
  错误。
- 必须重新编译全部目标平台 `libminicronet`，然后重新编译 Rust/Python native
  扩展并运行目标机测试。

验收：慢消费者只暂停自身请求/连接；恢复消费后数据连续且有序；取消、超时、
close 和进程退出无死锁；HTTP/WSS 高并发和大消息测试通过。

## 兼容性与发布策略

- 阶段 1--3 尽量保持 ABI v7；如果必须增加现有结构字段，只能使用 size/version
  前缀兼容规则并重新审计。
- 阶段 4 虽然主要是 Core 调度变更，也必须重新构建 Core 二进制，不能只替换 Rust
  或 Python 文件。
- 阶段 5 是 ABI v8，不能使用旧 Core 二进制混合发布；八个平台必须成套升级。
- 在 ABI v8 发布前，WebSocket 队列超限的安全策略是取消并报错，不是无限缓存或
  静默丢帧。
- Python 3.6 与主版本保持相同的公开语义；仅 PyO3 GIL API 和 native 模块名称
  按版本分别实现。

## 总体验证矩阵

- 同步：GIL 释放、完整响应、流式迭代、取消、超时、close。
- 异步：32/128/1000 并发、Future 唤醒、取消、事件循环关闭、单 recv 约束。
- 资源：响应大小上限、HTTP body 队列上限、WebSocket 消息/字节上限、引用释放。
- Core：多请求回调隔离、HTTP/WS pause/resume、H1/H2/H3、代理、TLS、WSS。
- 平台：Linux、Windows、macOS 各架构的 ABI、导出符号、依赖和真实加载测试。
