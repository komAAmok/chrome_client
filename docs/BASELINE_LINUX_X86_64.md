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

| 指标 | 旧 Core `7ed677ae` | 新 Core `b7eed689` |
| --- | --- | --- |
| 同步顺序 1 KiB，p50 延迟 | 1.30 ms | 1.38 ms |
| 同步顺序 1 KiB，吞吐 | 738.8 req/s | 701.7 req/s |
| asyncio 并发 32 | 682.5 req/s | 627.3 req/s |
| asyncio 并发 128 | 322.3 req/s | 307.9 req/s |
| 流式 64 MiB（64 KiB 分块读） | 148.5 MiB/s | 138.9 MiB/s |
| 慢消费者隔离，同 Engine | **永久挂死** | 20/20 完成，p50 1.28 ms |
| 慢消费者隔离，另一 Engine | 未能到达 | 20/20 完成，p50 1.36 ms |

新 Core 在纯吞吐上慢约 5%（每请求创建独立 sequenced runner 的代价），换来的是
慢消费者不再拖垮其它请求。

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
  也就是说提高并发反而降低吞吐。怀疑与进程级唯一网络线程加上每个事件都要绕一次
  事件循环的 `poll_event` 有关，需要在设计优化阶段实测确认。
- 上面的 42 ms 教训说明基准数字必须先做服务端对照，否则很容易把测量工具的缺陷
  当成被测对象的缺陷。

## 复现命令

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

- `minicronet/BUILD.gn` 与 smoke/probe 源码（`smoke.c`、`websocket_smoke.c`、
  `abi_cpp_smoke.cc`、`profile_probe.c` 等）。`tools/sync-core.sh` 只校验它们存在。
- profile 证据与 `profiles/normalized_profiles.json` 等三个输入，因此
  `profile_table_generated.h` 目前作为已提交的生成产物使用，重新生成需要显式
  设置 `REGENERATE_PROFILE_TABLE=1` 和 `PROFILE_EVIDENCE_DIR`。
- 抓包证据与 wire 校验工具链。

所以 `docs/MIGRATION_FROM_NEW.md` 里「目标仓库不再依赖 new/ 才能构建」这条门槛
仍未达成，收尾放在阶段 4。

## 布局差异

本仓库把 Core 头文件放在 `core/source/minicronet/`、ABI 头放在 `core/abi/`、导出表
放在 `core/exports/`；Chromium 则从自己的源码根解析 `#include "minicronet/x.h"`，
要求同目录平铺。`tools/sync-core.sh` 负责这个转换：把 13 个 Core 源文件、ABI 头和
三份导出表平铺进 `$CHROMIUM_SRC/minicronet/`。

两边的 `BUILD.gn` 因此不同 —— 本仓库那份用嵌套路径和 `../abi`、`../exports`，
Chromium 那份用平铺路径并且额外包含 8 个 smoke/probe 目标。同步脚本不覆盖
Chromium 侧的 `BUILD.gn`，只校验它存在。这是当前的已知分叉点。

