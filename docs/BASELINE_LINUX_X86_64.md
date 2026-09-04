# Linux x86_64 Core 基线（阶段 0）

本文记录精简、背压和设计优化工作开始前的可测量起点。所有数字都可以用
`tools/build-core-linux-x86_64.sh` 和 `tools/bench-core-baseline.py` 重现。

## 可复现性

Chromium 源码树 HEAD 与 `CHROMIUM_REVISION` 逐字符一致
（`010786339149198c8c24d58c30cf5a41fcf60c14`），11 个补丁均已应用。

| 构建输入 | SHA-256 | 大小 | 说明 |
| --- | --- | --- | --- |
| 08-23 版 Core 源码 | `7ed677ae…41c791` | 9,033,552 | 与 `core/binaries/linux-x86_64/manifest.json` 和已发布 wheel **逐字节一致** |
| 当前 `core/source/` | `b7eed689…f44ab20` | 9,033,552 | 阶段 1 起的参照基线 |

用原始输入重建能精确还原已发布产物，说明本机构建环境是确定的；随后对当前源码
连续两次强制重编译也得到同一哈希。因此后续 A/B 对比中的体积差异可以归因于改动
本身，而不是构建噪声。

两者体积相同、哈希不同：差异只是 Request/WebSocket 从进程级共享 callback runner
改为每请求独立 runner，指令数没有变化。

增量重建一次约 23 秒（ThinLTO 缓存 202 MB 已预热），A/B 实验成本很低。
strip 前 14,866,992 字节，strip 后 9,033,552 字节，即 `--strip-unneeded` 加上
移除 `.comment`/`.note.gnu.build-id` 去掉 5.83 MB。移除 build-id 是产物可复现的
前提，因为 build-id 哈希的是未 strip 的镜像。

## 体积构成

| 段 | 字节 | 占比 |
| --- | --- | --- |
| `.text` | 6,146,857 | 68.0% |
| `.rodata` | 2,082,303 | 23.1% |
| `.rela.dyn` | 432,240 | 4.8% |
| `.data.rel.ro` | 219,184 | 2.4% |
| `.bss` | 92,120 | — |
| `.data` | 65,128 | 0.7% |
| `.eh_frame` + `.eh_frame_hdr` | 46,500 | 0.5% |
| `.dynsym` | 7,920 | — |

无 `.symtab`、无调试段。导出符号 19 个，由 `minicronet.lds` 版本脚本限定。

运行时依赖（`DT_NEEDED`，9 项）：`libdl.so.2`、`libpthread.so.0`、`libnspr4.so`、
`libnss3.so`、`libnssutil3.so`、`libm.so.6`、`libgcc_s.so.1`、`libc.so.6`、
`ld-linux-x86-64.so.2`。

manifest 另外声明了 `libplc4.so` 和 `libplds4.so`，但它们不在 `DT_NEEDED` 里 ——
是 `libnss3` 的传递依赖，属于过度声明。

`tools/audit-core-linux.sh` 的体积上限是 9,050,000 字节，当前只剩约 16 KB 余量。
阶段 1 的 ABI v8 若增加代码会触发这个上限，需要同步调整并说明理由。

## 基准数字

本地 HTTP mock server，`bindings/python` release 扩展，Python 3.12.3。

测量前踩过一个坑：Python `BaseHTTPRequestHandler` 在 keep-alive 下把响应头和响应体
分成多次小写入，触发 40 ms 延迟 ACK 定时器，使每个请求凭空多出约 41 ms。对照实验
证实这是服务端问题而非客户端问题 —— 标准库 `http.client` 复用连接时 p50 为
41.0 ms，每次新建连接时只有 1.0 ms。基准脚本因此关闭 Nagle 并把小响应合并成一次
写入。任何改写这个 handler 的人都要保持这两点，否则数字会被服务端噪声淹没。

| 指标 | 旧 Core `7ed677ae`（v7 已发布） | 重建 v7 `b7eed689` | ABI v8 `5115bf07` |
| --- | --- | --- | --- |
| 同步顺序 1 KiB，p50 延迟 | 1.30 ms | 1.38 ms | 1.20 ms |
| 同步顺序 1 KiB，吞吐 | 738.8 req/s | 701.7 req/s | 804.6 req/s |
| asyncio 并发 32 | 682.5 req/s | 627.3 req/s | 579.6 req/s |
| asyncio 并发 128 | 322.3 req/s | 307.9 req/s | 315.7 req/s |
| 流式 64 MiB（64 KiB 分块读） | 148.5 MiB/s | 138.9 MiB/s | 150.9 MiB/s |
| 慢消费者隔离，同 Engine | **永久挂死** | 20/20，p50 1.28 ms | 20/20，p50 1.35 ms |
| 慢消费者隔离，另一 Engine | 未能到达 | 20/20，p50 1.36 ms | 20/20，p50 1.32 ms |

重建的 v7 在纯吞吐上比已发布版慢约 5%（每请求创建独立 sequenced runner 的代价），
换来慢消费者不再拖垮其它请求。ABI v8 把这部分代价拿回来还有盈余：同步吞吐和流式
吞吐都超过已发布的 v7，因为 pause/resume 取消了 Condvar 阻塞和随之而来的线程唤醒。
asyncio 并发路径在三者之间的差异处于运行间抖动范围内，没有明确趋势。

### 已发布 Core 的挂死可复现

`tools/bench-core-baseline.py` 的 `stalled_consumer_isolation` 用例是这项工作的
验收探针。它开一个流式请求、只取一块就停止消费，让 Rust 侧 body 队列停在 1 MiB
上限，Core 的回调线程随即阻塞在 `on_body` 里。

在已发布的 `7ed677ae` 上，之后**同一个 Engine 上的任何新请求都不会返回**，阻塞点是
`bindings/python/chrome_client/_python_impl/__init__.py:405` 的
`request.start_stream()`：响应回调排在被阻塞的回调之后，而两者共享
`core/source/engine.cc:186` 那个进程级唯一 sequenced runner。

更严重的是 `timeout=3` 也失效 —— Core 的超时确实触发，但送达超时结果的完成回调
同样排在这条被堵住的队列里。所以在这个状态下超时机制是不可用的，不只是变慢。

在重建的 `b7eed689` 上（`core/source/request.cc:114` 给每个请求建独立 runner），
两个 Engine 的探针都是 20/20 完成、p50 约 1.3 ms。

### 两个待查项

- **并发 128 比并发 32 慢一倍以上**（322 vs 683 req/s），两个 Core 都是如此。
  已在阶段 3 定位，见下文「并发倒挂」——原因是基准让所有并发请求打同一个 URL，
  而 HttpCache 对同一 cache key 的事务是串行的。
- 上面的 42 ms 教训说明基准数字必须先做服务端对照，否则很容易把测量工具的缺陷
  当成被测对象的缺陷。**并发倒挂是同一类错误的第二次出现。**

## 并发倒挂：是基准打同一个 URL，不是网络线程

「并发 128 比并发 32 慢一倍」从阶段 0 挂到现在，原先怀疑进程级唯一网络线程加上
每事件绕一次事件循环的 `poll_event`。两条怀疑都不成立。

### 排除 Python

用安全 Rust 层直接写一个多线程压测（无 Python、无 GIL、无 asyncio）仍然复现同样的
形状，所以问题不在绑定层：

| 并发 | Rust 直连 | Python asyncio |
| --- | --- | --- |
| 1 | 1,045 req/s | 611 req/s |
| 8 | 1,360 req/s | 1,145 req/s |
| 32 | 935 req/s | 767 req/s |
| 128 | 510 req/s | 340 req/s |

Python 让绝对值降低约 30%，但倒挂的拐点和幅度一样。

### 排除网络线程

网络线程确实是**一个**天花板：采样 `/proc/<pid>/task/*/stat` 显示 `MiniCronetNet`
稳定在单核的 88–93%，吞吐上限约 2,250 req/s。但它不是倒挂的原因——如果是，加
Engine 就该无效，实测相反：

| 配置 | 吞吐 |
| --- | --- |
| 1 Engine × 并发 8 | 2,255 req/s |
| 2 Engine × 并发 8 | 2,255 req/s |
| 4 Engine × 并发 8 | 2,227 req/s |

（所有 Engine 共享同一个 `base::Thread`，所以加 Engine 不加吞吐；但在无缓存竞争时
并发 128 也不掉，说明线程饱和只是限高，不产生倒挂。）

### 真正的原因：HttpCache 同 key 串行

固定并发 128，只改「有多少个不同的 URL」：

| 不同 URL 数 | 吞吐 | p50 |
| --- | --- | --- |
| 1 | 492 req/s | 278 ms |
| 2 | 667 req/s | 213 ms |
| 4 | 1,096 req/s | 125 ms |
| 8 | 1,604 req/s | 75 ms |
| 16 | 1,665 req/s | 74 ms |
| 128 | 1,628 req/s | 74 ms |

吞吐随不同 cache key 的数量近线性上升到约 8 个，然后撞上网络线程的限高。三项对照
确认这是缓存层而非协议层：

| 条件 | 并发 8 → 128 |
| --- | --- |
| 缓存开 + 同一 URL | 1,346 → 523 req/s（倒挂） |
| 缓存开 + 每请求不同 URL | 1,524 → 1,617 req/s（平） |
| **缓存关** + 同一 URL | 2,286 → 2,254 req/s（平） |
| 缓存开 + 同一 URL + 响应带 `Cache-Control: no-store` | 1,400 → 295 req/s（**仍然倒挂**） |

最后一行是关键：即使响应不可缓存，倒挂依然存在。所以不是「写缓存条目」的开销，而是
`HttpCache` 按 cache key 排队——同 key 的事务在 `ActiveEntry` 上串行，`no-store`
只是让它们轮流 doom 条目，队列照旧。

结论：**并发超过不同 URL 的数量之后不再增加并行度，只增加队列深度**，所以吞吐下降、
延迟线性上升。这不是缺陷，是 Chromium 的缓存语义；真实 Chrome 同样如此。

### 基准已修正

`tools/bench-core-baseline.py` 原先让 200 个并发请求全打
`/small?size=1024`，于是 `async_concurrency_128` 测的是缓存串行而不是并发能力。
现在每个请求带一个 `n=` 序号，形状随之正常：

| 指标 | 修正前（同一 URL） | 修正后（不同 URL） |
| --- | --- | --- |
| 并发 32 | 737 req/s | 1,294 req/s |
| 并发 128 | 377 req/s | 1,227 req/s |

`--single-url-concurrency` 保留下来，用于故意复现这个竞争。

### 一个没有收益的尝试

顺手试过让 asyncio 侧的 `notify` 每次唤醒最多排空 64 个事件（原先一次一个）。
零收益：加了插桩数一下每次唤醒实际排空几个事件，请求路径 830/864 次只有 1 个，
流式路径 2,063/4,255 次只有 1 个、超过 2 个的不到 1%。Core 本来就是一到一送，
批量排空无从生效。已丢弃，不进仓库。

## ICU 与 IDN：从进程级崩溃到内嵌 IDNA 数据

排查 Windows wheel 里那份 10.8 MB `icudtl.dat` 时发现，**Core 从不调用
`base::i18n::InitializeICU()`**，但 `net`/`url` 仍把 ICU 链进依赖图。后果不是体积
浪费而已：

```
chrome_client.get("http://例え.テスト/")   # 曾经 SIGTRAP，整个宿主进程被终止
```

信号 5（SIGTRAP，Chromium 的 `IMMEDIATE_CRASH`）来自 URL 规范化 —— 非 ASCII 主机名
需要 IDNA，而 ICU 数据从未注册。任何调用方传入国际化域名都会打掉宿主进程。

### 三种数据集的实测体积

用同一份 ICU 源码（`third_party/icu/source/data`，102 MB）配不同过滤器构建：

| 数据集 | 大小 | 说明 |
| --- | --- | --- |
| Chromium `common/icudtl.dat` | 10,876,560 | 完整数据 |
| IDNA + 全部字符集转换器 | 6,276,640 | `core/icu/filter-idna-plus-uconv.json` |
| **仅 IDNA（采用）** | **191,056** | `core/icu/filter.json` |

字符集转换器占了第二种方案的 **97%**（6.09 MB）。内嵌它会让每个平台的库从 9.0 MB
涨到 15.3 MB，与轻量化目标相反；而 Core 把响应头以原始字节交给绑定层解码，从不调用
ICU 转换器。所以只保留 IDNA。这不是功能回退 —— 此前 ICU 完全没有初始化，没有任何
现有功能依赖过 ICU 转换器。

最终数据集 9 个条目：`uts46.nrm`、`nfkc.nrm`、`cnvalias.icu`、`uemoji.icu`、
`ulayout.icu`、`icustd.res`、`icuver.res`、`curr/supplementalData.res`、
`zone/tzdbNames.res`。生成方式见 `core/icu/README.md`。

### 各平台体积变化

数据用 `icu_use_data_file = false` 编入库中，不再随产物携带外挂文件。

| 目标 | 内嵌前 | 内嵌后 | 增量 |
| --- | --- | --- | --- |
| linux-x86 | 8,614,628 | 8,805,412 | +190,784 |
| linux-x86_64 | 9,033,552 | 9,320,288 | +286,736 |
| linux-arm64 | 8,432,256 | 8,717,840 | +285,584 |
| windows-x86 | 9,063,424 | 9,254,912 | +191,488 |
| windows-x86_64 | 11,266,560 | 11,457,024 | +190,464 |
| windows-arm64 | 9,313,792 | 9,504,256 | +190,464 |
| macos-x86_64 | 8,651,552 | 8,844,064 | +192,512 |
| macos-arm64 | 7,823,120 | 8,021,264 | +198,144 |

**Windows wheel 净减 10.69 MB**：库增加约 190 KB，但不再携带 10,876,560 字节的
`icudtl.dat`。Linux 和 macOS 各增加约 190--287 KB，换来此前会崩溃的 IDN 支持。

仓库里 `core/dependencies/windows-*/icudtl.dat` 三份共 32 MB 已删除，
`tools/audit-core-binaries.sh` 反向断言它们不得再出现。

`tools/audit-core-linux.sh` 的体积上限相应从 9,050,000 提到 9,400,000。

### IDN 现在真正可用

`mn_request_create` 保留了 `GURL(url).is_valid()` 校验，把 `not-a-url`、`http://`
这类畸形 URL 拦在创建期；`engine.cc` 在 `Runtime` 构造时初始化 ICU，失败则 Engine
创建返回 `MN_ERROR_INITIALIZATION_FAILED`，而不是留到规范化时崩溃。

验收标准是 IDN 主机名与其 punycode 形式行为一致：

| 输入 | 结果 |
| --- | --- |
| `http://例え.テスト/` | `Timeout`（net error -7，走到 DNS） |
| `http://xn--r8jz45g.xn--zckzah/` | `Timeout`（同上） |
| `http://<local>/path/路径?q=值` | 200，路径按百分号编码 |
| `not-a-url` | `RequestException: InvalidArgument` |

`test_internationalized_host_is_canonicalized` 覆盖这四种情况。

内嵌 ICU 后的基准（同口径）：同步顺序 833.5 req/s、p50 1.17 ms；流式 152.8 MiB/s；
并发 32 为 742.0 req/s、并发 128 为 363.4 req/s；慢消费者隔离两个 Engine 均 20/20。
与 ABI v8 内嵌前相比没有退化。


```sh
# 重建并审计（增量约 23 秒）
RG=$(command -v rg) tools/build-core-linux-x86_64.sh

# 基准：先 release 构建 Python 扩展，再分别指向两个 Core
MINICRONET_CORE_DIR=$PWD/core/binaries/linux-x86_64 \
  cargo build --release -p chrome-client-python
export PYTHONPATH=$PWD/bindings/python:$PWD/target/release
LD_LIBRARY_PATH=$PWD/core/binaries/linux-x86_64 \
  tools/bench-core-baseline.py --skip-isolation   # 已发布 Core，隔离用例会挂死
LD_LIBRARY_PATH=/home/sj/chromium/src/out/MiniCronet-linux-x86_64 \
  tools/bench-core-baseline.py                    # 重建 Core，含隔离用例
```

## `is_cfi = false` —— 减 147,456 字节

Chromium 在 `build/config/sanitizers/sanitizers.gni:58` 只对
`is_official_build && is_clang && linux-x64`（以及 ChromeOS 设备）默认打开
`is_cfi`，`use_cfi_icall` 的 cflags 也嵌在 `if (is_cfi)` 分支里。所以这个开关
**只影响 linux-x86_64 一个目标**。

| 段 | is_cfi=true | is_cfi=false | 差 |
| --- | --- | --- | --- |
| `.text` | 6,217,433 | 6,093,696 | −123,737 |
| `.eh_frame` + `.eh_frame_hdr` | 46,524 | 36,204 | −10,320 |
| `.data.rel.ro` | 223,952 | 217,872 | −6,080 |
| `.rela.dyn` | 444,192 | 439,320 | −4,872 |
| `.relro_padding` | 3,824 | 592 | −3,232 |
| `.rodata` | 2,280,447 | 2,281,215 | +768 |
| 文件总计 | **9,320,288** | **9,172,832** | **−147,456（−1.58%）** |

`cfi-vcall` 的跳转表占了绝大部分。同一份配置连续两次独立编译得到同一哈希
`3c09b8dd…14a0c0`，可复现性与 ICU 那轮一致。
`tools/audit-core-linux.sh` 的体积上限相应从 9,400,000 收到 9,250,000。

### 吞吐没有可测量的变化

在同一次会话里交替跑三轮（新旧 Core 都在本机，避免跨天机器状态差异），下表取
中位数并给出三轮区间：

| 指标 | is_cfi=true | is_cfi=false |
| --- | --- | --- |
| 同步顺序 1 KiB | 794.4 [769.7–797.4] req/s | 809.4 [770.8–810.3] req/s |
| 同步 p50 延迟 | 1.2 ms | 1.2 ms |
| asyncio 并发 32 | 760.6 [749.9–783.9] req/s | 733.0 [729.1–752.4] req/s |
| asyncio 并发 128 | 382.8 [377.5–404.9] req/s | 387.3 [377.6–395.5] req/s |
| 流式 64 MiB | 162.4 [158.4–164.0] MiB/s | 161.3 [144.4–162.7] MiB/s |
| 慢消费者隔离 | 3 × 20/20 | 3 × 20/20 |

区间互相重叠，没有方向一致的变化。这符合预期：热路径是网络 I/O，间接调用检查
不在瓶颈上。功能面 16 个 Python 用例全过（1 个因缺 WS 端点 skip），
`minicronet_smoke`、`run-http-smoke.py` 与 4 个 audit 脚本全过。

### 这是安全性换体积

去掉 `cfi-vcall` 和 `cfi-icall` 之后，一旦网络栈出现内存破坏漏洞，攻击者劫持
虚表或函数指针的门槛就降低了。用户已确认接受这个取舍。

### 其余 7 个目标：零改动，已实测

`is_cfi = false` 写进 8 个构建脚本只是为了参数统一。对非 linux-x64 目标它不改变
任何生成物：把这一行加进 `args.gn` 后重新 `gn gen`，
`obj/minicronet/minicronet.ninja` 与 `toolchain.ninja` 的 md5 和加之前完全相同
（已在 linux-arm64、windows-x86_64、macos-arm64 上实测），
`--fail-on-unused-args` 也不报错。剩下 4 个目标共用同一条默认值表达式，因此
这 7 个平台的已发布二进制无需重建。

## 阶段 3：删除 `Engine::callback_runner()` 死代码

ABI v8 让 Request 和 WebSocket 各自建独立 sequenced runner（`request.cc:113`、
`websocket.cc:186`），`Engine` 那条通往 `Runtime` 的进程级 runner 从此没有调用方。
这次删掉 `Engine::callback_runner()`、`Runtime::callback_runner()` 和
`Runtime::callback_runner_`，连带 `Runtime` 构造里那次
`base::ThreadPool::CreateSequencedTaskRunner({})` 和两个不再需要的 include。

八个平台重建后只有 linux-x86 缩了 64 字节（8,805,412 → 8,805,348），其余七个目标
每个段的大小都一字不差 —— 但 8 份产物的 SHA-256 全部变了。被删的三个访问器不在导出
表里，ThinLTO 与 `--gc-sections` 之后剩下的指令本就极少；真正的差异是 `Runtime` 少
一个 8 字节成员、构造函数少一次 runner 创建。这点差异在 7 个目标上被 `.text` 的 64
字节对齐填充吃掉，只有 linux-x86 越过了对齐边界。

所以这一步的收益不是体积，而是：每个进程不再创建一个没人使用的 sequenced task
runner，以及少了一条会让读者误以为「Engine 层有统一回调队列」的线索。

验收：8 个平台 audit 全过；linux-x86_64 上 16 个 Python 用例通过（1 skip）、
`minicronet_smoke` 与 `run-http-smoke.py` 通过；基准与删除前同口径
（同步 784.1 req/s、p50 1.22 ms、并发 32 为 727.6、并发 128 为 373.1、流式
159.1 MiB/s、慢消费者隔离两个 Engine 均 20/20），全部落在此前建立的抖动区间内。

### 行号会进产物：6 个字节的教训

删完之后为可读性补回一个空行，产物哈希又变了一次（`7d9192b0…` → `ca78db07…`），
体积不变。逐字节比对只有 **6 处不同，每处 +1**：都是 `FROM_HERE` 和 `CHECK` 展开出
来的 `__LINE__` 立即数。

也就是说任何让 `FROM_HERE`/`CHECK` 所在行号位移的编辑——包括纯空行——都会改变产物
哈希，而指令语义一个都没变。做 A/B 实验前必须确认两边源码行号一致，否则「哈希不同」
这个信号说明不了任何事。

## `enable_websockets`：不是逻辑错误，是缺注释

`net/features.gni` 里这一行长期被记成「逻辑写歪了，`||` 使后半段无作用」：

```gn
enable_websockets = !is_cronet_build || is_minicronet_build
```

重新核对后结论不同。两个 arg 相互独立（`build/config/cronet/config.gni` 里
`is_cronet_build = is_cronet_for_aosp_build`，`is_minicronet_build = false`），四种
取值组合下这个表达式都给出正确结果，第二个操作数是防御性的：MiniCronet 的 C ABI
导出 `mn_websocket_*`，WebSocket 编不进来 ABI 就残缺。8 个发布配置都不设
`is_cronet_build`，所以它在今天是恒真冗余，而不是错误。

补丁因此只加两行注释把这个约束写在原地。注释零影响已验证：加与不加各
`gn gen` 一次，out 目录里 1,674 个 `.ninja` 文件逐字节相同，
`gen/net/net_buildflags.h` 也相同。

## 已排除的精简候选

### `optional_trace_events_enabled = false` —— 零收益

Chromium 自己在 `base/trace_event/tracing.gni:18` 的注释说这个开关在 Android/ChromeOS
默认关闭是「due to binary size impact」，所以它是本轮排序里的第二候选。

实测结论是**没有任何影响**：

| 检查 | 结果 |
| --- | --- |
| `args.gn` 含该开关 | 是 |
| `gen/base/tracing_buildflags.h` | 翻转为 `(0)` |
| 产物 SHA-256 | `9a221045…` → `9a221045…`，**逐字节一致** |

也就是说 `OPTIONAL_TRACE_EVENT` 宏没有出现在任何进入这个精简 Core 的代码路径里，
`--gc-sections`、`--icf=all` 和 ThinLTO 已经把它们清干净了。这个候选可以从清单上
划掉，不必再试。

`tools/build-core-linux-x86_64.sh` 保留了 `DISABLE_OPTIONAL_TRACE_EVENTS=1` 开关，
方便以后 Core 范围扩大后重新测量。

## 本仓库尚未拥有的构建输入

阶段 0 迁入了 11 个补丁（`core/patches/`）、8 个平台构建脚本、3 个平台审计脚本、
FeatureList 审计和 profile 表生成器。

脚本的验证程度不同，不要混为一谈：

| 脚本 | 状态 |
| --- | --- |
| `tools/build-core-linux-x86_64.sh` | 已执行验证，产出可复现 |
| `tools/audit-core-linux.sh`、`audit-network-featurelist.sh` | 已执行验证 |
| 其余 7 个 `build-core-*.sh` | 只做了同构适配和语法检查，**未执行验证** |
| `tools/audit-core-{windows,macos}.sh` | 只做了路径适配和语法检查，**未执行验证** |

7 个未验证的构建脚本与已验证的 x86_64 版做了同样两处机械改动：profile 表重新生成
改为 `REGENERATE_PROFILE_TABLE=1` 显式开启，本地 pkgconf 改为 `MINICRONET_PKGCONF_DIR`
环境变量提供。它们在阶段 1 重建全部 8 个平台时才会被真正跑通。

仍然只存在于 `/home/sj/桌面/new` 或 `/home/sj/chromium/src` 的部分：

- 仅在 `minicronet_profile_verification=true` 下编译的 4 个校验探针
  （`profile_isolation_probe.c`、`profile_feature_probe.cc`、
  `profile_state_isolation_probe.cc`、`websocket_extended_connect_probe.c`），
  它们依赖 profile 证据流水线。`tools/sync-core.sh` 只校验它们存在。
- profile 证据与 `profiles/normalized_profiles.json` 等三个输入，因此
  `profile_table_generated.h` 目前作为已提交的生成产物使用，重新生成需要显式
  设置 `REGENERATE_PROFILE_TABLE=1` 和 `PROFILE_EVIDENCE_DIR`。
- 抓包证据与 wire 校验工具链。
- Linux x86/ARM64 交叉编译需要 `new/.tools/pkgconf`，通过
  `MINICRONET_PKGCONF_DIR` 指定；x86_64 用系统 pkg-config 即可。

发布路径（默认 args.gn，不含校验探针）已经完全由本仓库自有源码驱动：ABI v8 那轮
把 `BUILD.gn` 和 4 个 smoke/probe 源码迁入 `core/source/` 后重建，产物哈希与迁入前
一致，说明 chromium 树里不再残留手改。

所以 `docs/MIGRATION_FROM_NEW.md` 里「目标仓库不再依赖 new/ 才能构建」这条门槛
对发布路径已成立，对 profile 证据与校验探针仍未成立，收尾放在阶段 4。

## 布局差异

本仓库把 Core 头文件放在 `core/source/minicronet/`、ABI 头放在 `core/abi/`、导出表
放在 `core/exports/`；Chromium 则从自己的源码根解析 `#include "minicronet/x.h"`，
要求同目录平铺。`tools/sync-core.sh` 负责这个转换：把库源码、smoke/probe 源码、
`BUILD.gn`、ABI 头和三份导出表平铺进 `$CHROMIUM_SRC/minicronet/`。

`core/source/BUILD.gn` 现在就是 Chromium 编译的那一份（平铺路径），不再有仓库本地
的嵌套变体，所以只有一处需要维护。

