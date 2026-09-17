# Core 二进制体积精简方案

状态：提案（2026-09-17 实测基线）。本文是 `docs/BASELINE_LINUX_X86_64.md` 的后续：
那份文档记录了已完成的精简（ICU IDNA 内嵌、`is_cfi=false`、磁盘缓存裁剪等）和已排除
的候选，本文不再重复，只覆盖**当前还剩下的可做项**。

## 一、现状分析：基线量化方法与实测数据

### 1.1 量化方法（可复现命令）

| 层面 | 工具 | 命令要点 |
| --- | --- | --- |
| 段级 | `readelf -S -W` | 解析节头表，按段求和；8 平台各跑一次 |
| 符号级 | `llvm-nm --size-sort` | **必须在未 strip 镜像上做**：删 `out/<dir>/libminicronet.so` 后 `ninja minicronet:minicronet` 重链，分析完 `llvm-objcopy --strip-unneeded --remove-section=.comment --remove-section=.note.gnu.build-id` 恢复 |
| 命名空间级 | `llvm-cxxfilt` demangle 后聚合 | `net::` / `base::` / `quic::` / `bssl::` / `std::` 等 |
| Windows 段 | `llvm-readobj --sections` | `.text`/`.rdata`/`.pdata` |
| 依赖级 | `readelf -d`（DT_NEEDED） | NSS/NSPR 等动态依赖 |

注意：**仓库里的产物是 strip 过的**，段级分析有效但符号级必须重链未 strip 镜像
（约 30 秒）。A/B 对比前必须对齐源码行号——`FROM_HERE`/`CHECK` 会把 `__LINE__` 编进
产物，纯加空行也会改哈希（见 BASELINE 文档「6 个字节的教训」）。

### 1.2 段级实测（当前 revision b75a5a95，2026-09-17）

**linux-x86_64（9,045,864 字节）**：

| 段 | 字节 | 占比 |
| --- | --- | --- |
| `.text` | 5,970,565 | 66.0% |
| `.rodata` | 2,273,879 | 25.1% |
| `.rela.dyn` | 440,712 | 4.9% |
| `.data.rel.ro` | 218,360 | 2.4% |
| `.bss` | 93,352 | — |
| `.data` | 65,152 | 0.7% |
| `.eh_frame` | 30,748 | 0.3% |

linux-x86（8,641,720）与 linux-arm64（8,578,904）结构一致（`.text` ≈ 65–71%，
`.rodata` ≈ 25%）。**windows-x86_64（11,391,488）的 `.text` 高达 8,409,088（73.8%）**，
比同源码的 Linux x86_64 大 41%：来源是 x64 Windows ABI 的 SEH 函数序言
（每个函数的 unwind 记录指令）、更大的指令编码（shadow space、`mov` 代替 `push`）
和 16 字节函数对齐；`.pdata`（unwind 表）另占 227,328。这是平台固有开销，
gn 参数与 Linux 完全一致（`is_official_build=true` 已默认开 `/OPT:REF,ICF`），
没有等效于 `--icf=all` 级别的进一步开关。

### 1.3 符号级实测（未 strip 镜像，符号覆盖 8,686,746 字节 / 38,304 个符号）

按命名空间：

| 命名空间 | 字节 | 占符号覆盖 |
| --- | --- | --- |
| `net::`（网络栈本体） | 3,456,907 | 39.8% |
| `std::`/C++ 标准库实例化 | 2,051,612 | 23.6% |
| `base::` | 1,423,556 | 16.4% |
| `quic::` | 819,826 | 9.4% |
| `bssl::`（BoringSSL） | 587,606 | 6.8% |
| `simdutf`（UTF 转换） | 380,752 | 4.4% |
| partition_alloc/shim | 289,037 | 3.3% |
| ICU 静态属性表 | 287,338 | 3.3% |
| `icu_78::`（ICU 本体） | 228,177 | 2.6% |
| perfetto/track-event | 167,599 | 1.9% |

Top 符号：

| 符号 | 字节 | 性质 |
| --- | --- | --- |
| `net::kPreloadedHSTSData` | 753,232 | HSTS 预载表（huffman 紧凑格式） |
| `kBrotliDictionaryData` | 122,784 | Chrome 通告 `br`，指纹必需 |
| `propsVectorsTrie_index` | 86,264 | ICU 属性 trie（编译进库本体） |
| `net::registry_controlled_domains::kDafsa` | 51,764 | public suffix list，cookie 域规则必需 |
| `propsTrie_index` | 48,216 | ICU 属性 trie |
| `propsVectors` | 27,252 | ICU 属性向量 |
| `ucase_props_trieIndex` | 27,088 | ICU 大小写映射 trie |
| `ubidi_props_trieIndex` | 26,776 | ICU 双向类别 trie |
| `icu_78::PropNameData::nameGroups` | 24,665 | ICU 属性名查询表 |
| `base::perfetto_track_event::kCategories` | 19,264 | 静态 trace 类别表 |
| `net::kRootCerts` | 18,428 | Chrome 根证书集 |
| `bssl::kOpenSSLReasonStringData` | 15,611 | OpenSSL 错误字符串 |

### 1.4 关键发现

1. **审计上限已经贴线**：linux 9,050,000（余 4,136）、windows-x86_64 11,400,000
   （余 8,512）、macos 8,750,000（余 37,200）。**任何新增 profile / feature 都会触发
   门禁失败**。精简不只是省体积，也是给后续开发留余量。
2. **新发现：ICU 属性表约 245 KB 编译进 libicu 本体**，不随 IDNA-only 数据集裁剪
   （`propsVectorsTrie` 86K + `propsTrie` 48K + `propsVectors` 27K + `ucase` 27K +
   `ubidi` 27K + `PropNameData` 30K）。其中 UTS #46（IDNA）真正可能需要的只有
   case mapping 与 bidi 类别；`PropNameData`（属性名查询）在运行时几乎不可能被触达。
3. 已完成的常规手段全部生效中：`optimize_for_size=true`（-Os）、ThinLTO、
   `--gc-sections`、`--icf=all`、`--strip-unneeded` + 移除
   `.comment`/`.note.gnu.build-id`、磁盘缓存双后端早返回 patch（8 平台 −2.56%）。

## 二、方案清单

排序原则：先按实施风险（低→高），同级内按收益（大→小）。每项独立成 commit，
均可单独回退。

### P0-1 HSTS 预载表裁剪 —— −786,432 字节（−8.69%）｜风险：产品决策

- **做法**：8 个 `tools/build-core-*.sh` 的 args.gn 各加
  `include_transport_security_state_preload_list = false`，同步下调三档审计上限、
  更新 `docs/COMPATIBILITY_BOUNDARY.md`。
- **实测收益**：linux-x86_64 9,045,864 → 约 8,259,432；8 平台合计约 −6.3 MB。
- **副作用（这就是为什么它需要拍板）**：Chrome 会在发出任何字节前把预载域名的
  `http://` 升级为 `https://` 并执行 HPKP 钉扎。去掉后这些请求真的以明文发出：
  既是安全回退，也是**可被检测方观察到的与真实 Chrome 的行为差异**
  （`kPreloadedHSTSData` 已是 huffman 压缩格式，无法只压不删）。
- **折中选项**：出「no-preload」变体 SKU 与主 SKU 并行发布，主 SKU 不变。

### P1-1 链接器页对齐收紧 —— 预期 −4~60 KB/平台｜风险：低

- **做法**：`core/patches/minicronet-core.patch` 中 minicronet 链接参数加
  `-z noseparate-code`（可再试 `-z max-page-size=4096`，须验证 manylinux2014
  加载器行为）。lld 默认 `separate-code` 把 LOAD 段按 2 MB 边界排布，
  关闭后段间 padding 减少。
- **验证**：`readelf -lW` 对比 program header 排布；smoke + Python 套件。
- **副作用**：代码页与只读数据共享页边界，W^X 粒度下降——对一个只有 20 个 C 入口
  的共享库影响有限，但需要在 commit message 里写明。Windows/macOS 无对应项。

### P1-2 `-Oz` 实验（比 -Os 更激进）—— 预期 −1~3%｜风险：低（可逆）｜收益待实测

- **做法**：`optimize_for_size` 之上试 `treat_warnings_as_errors` 保持、
  在 `core/patches/minicronet-core.patch` 给 minicronet target 追加 `-Oz`
  （只影响本库+net 的受控范围不可行时，退为全 target 实验）。
- **验证**：A/B 两次构建比体积 + `bench-core-baseline.py` 同口径三轮
  （同步吞吐 / 流式 / 慢消费者隔离）。吞吐区间重叠才采纳。
- **副作用**：`-Oz` 禁用部分向量化与循环展开，TLS/QUIC 热路径可能回退 2–5%；
  基准不达标即弃。

### P2-1 perfetto/track-event 裁剪 —— −167,599 字节（−1.85%）｜风险：中

- **背景**：`enable_base_tracing=false` 这个 gn 参数在本 revision 已不存在，
  无开关可用。
- **做法**：仿磁盘缓存 patch——定位 `base::perfetto_track_event` 的静态注册链
  （`kCategories` 19K 与 TrackEvent 初始化引用点），用
  `BUILDFLAG(MINICRONET_BUILD)` 早返回让 `--gc-sections` 自然清掉。
  不删源码清单，保留「未来若被引用构建仍通过」的性质。
- **副作用**：Core 无 trace 能力（本来也没有消费方）。
- **验证**：smoke + Python 套件 + `llvm-nm` 确认符号归零。

### P2-2 ICU 静态属性表裁剪 —— 预期 −30~250 KB｜风险：中（正确性相关）

- **背景**：新发现（见 1.4-2）。这些表编在 libicu 本体里，数据集过滤器管不到。
- **做法**（分两步，每步独立验证）：
  1. **`PropNameData`（约 −30 KB，风险低）**：属性名查询 API
     （`u_getPropertyValueName` 一类）没有 ABI 调用方，patch `third_party/icu` 的
     `propname.cpp` 在 `MINICRONET_BUILD` 下返回空表。
  2. **`propsVectorsTrie`/`ucase`/`ubidi`（约 −215 KB，风险中）**：UTS #46 需要
     case mapping 与 bidi 类别，**不能直接删**；做法是先 patch 掉后跑
     `test_internationalized_host_is_canonicalized`（IDN 四用例）+ `uts46` 实测，
     失败则还原。`propsTrie`/`propsVectors`（75 KB）是通用属性查询，
     需审计 `u_getIntPropertyValue` 调用图后决定。
- **验证**：IDN 用例（例え.テスト 与 punycode 等价性）+ 全套回归。

### P2-3 BoringSSL 错误字符串裁剪 —— −15,611 字节（−0.17%）｜风险：低

- **做法**：`bssl::kOpenSSLReasonStringData` 是 `ERR_reason_error_string` 的数据。
  Rust 侧错误映射走 Chromium net error 码，不读这些字符串；patch 成空表。
- **验证**：Python 证书错误用例（错误码消息仍来自 net error）+ smoke。

### P3-1 simdutf 替换 —— 380,752 字节（−4.21%）｜风险：高（不建议现在做）

- **现状**：`base::WideToUTF8` 等的 UTF-8/16/32 实现。--gc-sections 之后仍剩
  380 KB，说明它是**被 net/base 实际引用的活代码**，裁剪 = 换实现而非删死码。
- **如果要做**：把 base 的 UTF 转换入口在 `MINICRONET_BUILD` 下改指 ICU
  （已内嵌）或手写最小实现，逐函数对照 fuzz。正确性风险与工作量都不匹配收益，
  建议只在 HSTS 也不采纳、且必须突破 −10% 目标时再评估。

### 不可行项（已排除，列出防止重试）

| 项 | 结论 | 原因 |
| --- | --- | --- |
| brotli 字典 / zstd 解码表（−239 KB） | 不可行 | Chrome 通告 `br`/`zstd`，指纹必需 |
| QUIC/H3 裁剪（quic:: 820 KB） | 不可行 | 产品核心功能（chrome profile 的 h3） |
| kDafsa PSL（−52 KB） | 不可行 | cookie 域规则正确性必需 |
| DT_RELR 压 `.rela.dyn`（−390 KB） | 已排除 | manylinux2014 = glibc 2.17 加载器不支持 |
| NSS/NSPR 动态依赖移除 | 已决策不做 | 上游编译不过 + 改变证书校验语义 |
| `exclude_unwind_tables` | 更正：无需决策 | official 构建默认**已经是**排除状态，编译命令里就是 `-fno-unwind-tables -fno-asynchronous-unwind-tables`（见 6.7）。残留的 `.eh_frame` 30,748 字节来自汇编与 `.cfi` 指令，不归这个开关管 |
| libgcc_s 静态化 | 不建议 | 省一个 DT_NEEDED 但净增体积，无合规收益 |
| `optional_trace_events_enabled=false` | 已排除 | 实测零收益（已 strip 干净） |
| C++ 模板/std:: 膨胀治理（2.05 MB） | 已尽力 | `--icf=all` + ThinLTO 生效后是真实使用量 |

### 动静态链接取舍（现状即最优）

协议栈本体（BoringSSL/ICU/quiche/net/base）静态编入单一产物是项目前提：指纹一致、
无外部依赖漂移、分发零外置（Windows wheel 曾因外置 `icudtl.dat` 踩坑后删除）。
动态项仅 NSS/NSPR（真实 Chrome 读系统信任库，语义必需）与 libc/libstdc++/libm
（manylinux 合规）。**没有可动的空间**，列此防止反复讨论。

## 三、约束

1. **功能与接口兼容**：ABI v8 的 20 个导出符号、签名、版本协商不变；所有精简
   必须通过 `tools/audit-abi.sh` 与 `audit-core-binaries.sh`（8 平台）。
2. **指纹红线**：每项落地前后跑 `tools/inspect-client-hello.py`（≥4 次采样，
   横跨 99–153 的 11 个 profile），cipher 数、稳定扩展集合、profile 间差异
   逐字节一致。HSTS 项额外要在 COMPATIBILITY_BOUNDARY 里显式声明行为差异。
3. **性能红线**：`tools/bench-core-baseline.py` 同口径——同步顺序吞吐、
   asyncio 并发 32/128（每请求不同 URL）、流式 64 MiB、慢消费者隔离 20/20。
   `-Oz` 与任何代码路径改动的验收标准是三轮区间与新基线重叠。
4. **增量实施与回退**：一项 = 一个 commit（源码 + 8 平台二进制 + manifest +
   审计上限同改）。回退 = `git revert` 单个 commit，二进制随源码一起回。
5. **可复现性**：A/B 前对齐 `FROM_HERE`/`CHECK` 行号；每轮记录 strip 前后双哈希。

## 四、验证与防膨胀

### 4.1 每步的验证流程（固定顺序）

```
1. 重链 + strip + 8 平台 audit（体积上限按本步预期同步收紧）
2. minicronet_smoke / run-http-smoke.py / websocket smoke（linux-x86_64）
3. cargo test --workspace（8 个单元测试，含背压）
4. Python 套件 103 用例（test_stability + test_compat）
5. inspect-client-hello.py 指纹对照（改动触及行为时必跑）
6. bench-core-baseline.py（触及代码路径或优化等级时必跑）
7. install-core-binaries.py + 体积对比表写入本文档
```

### 4.2 防膨胀自动化（在已有门禁上补三件事）

1. **上限随精简收紧**（已有机制，执行纪律问题）：每完成一项，三档 MAX_BYTES
   同步下调到「新体积 + 2%」，结束「贴线 4 KB」状态。
2. **段级漂移监控（新增）**：`tools/audit-core-linux.sh` 增加段级断言——
   `.text`/`.rodata` 超过基线 +1% 即失败，比总字节数更早发现代码膨胀。
3. **profile 表增量门禁（新增）**：当前 35 KB / 55 个 profile（trust anchor 数组
   占 186 B×N）。每个新 profile 的合理增量 < 1 KB（共享去重已生效）；把
   「单 profile 增量」写进 audit，防止未来 profile 泛滥。

### 4.3 收益汇总（linux-x86_64，按采纳组合）

| 组合 | 预期体积 | 相对当前 |
| --- | --- | --- |
| 当前 | 9,045,864 | — |
| P1（padding + -Oz 达下限） | ≈ 8,955,000 | −1.0% |
| P1 + P2 全部（不含 HSTS） | ≈ 8,505,000 | −6.0% |
| 全部含 HSTS | ≈ 7,720,000 | **−14.7%** |

（P2 各项按符号量等比映射到其余 7 平台；Windows 的 `.text` 平台开销按 41%
差值同向缩放。每项以实测为准，上表是排期用的量级估计。）

## 五、待确认项

1. **目标体积**：本方案未收到明确目标。若目标是 −10% 以上，HSTS（P0）必须纳入；
   若目标在 −5% 内，P1+P2 即可达成且零行为差异。
2. **HSTS 决策**：主 SKU 裁剪、保留、还是出 no-preload 变体，需要产品拍板。
3. **P2-2 第二步（ICU 属性 trie）**：是否值得投入正确性审计，取决于目标体积。

## 六、执行结果（2026-09-17，8 平台实测）

基线是已提交的 `core/binaries/*`（rev b75a5a95；linux-x86_64 为 9,045,864 字节，与
1.2 节一致）。所有对比都在各自的 `out/MiniCronet-*` 上增量构建，Linux 侧 strip 命令与
`tools/build-core-*.sh` 逐字一致（`--strip-unneeded` + 移除
`.comment`/`.note.gnu.build-id`），每项单独构建、单独量。

| 项 | 预期 | linux-x86_64 实测 | 结论 |
| --- | --- | --- | --- |
| P1-2 `-Oz` | −1~3% | **−258,048 字节（−2.85%）** | 采纳 |
| P1-1 `-z noseparate-code` | −4~60 KB | **0 字节** | 保留，但只是显式钉住布局 |
| P2-2 第一步 PropNameData | −30 KB | **−32,768 字节（−0.374%）** | 采纳 |
| P2-1 perfetto/track-event | −167,599 字节 | **0 字节** | 不采纳（未过验收，已回退） |
| P2-2 第二步 属性 trie | −215 KB | 未实施 | 证据表明不可行 |
| P2-3 BoringSSL 错误串 | −15,611 字节 | −16,384 字节（可做） | 产品侧不执行，已回退 |
| **P1-3 Windows `optimize_for_size`** | 计划外新发现 | **−1,203,200 字节（−10.56%）** | 采纳，见 6.8 |
| **采纳组合** | ≈ −1% | **−286,720 字节（−3.17%）** | linux-x86_64 = **8,759,144** |

八平台合计 **72,700,328 → 68,869,624 字节（−3,830,704，−5.27%）**，逐平台数字见 6.8。

采纳组合不是各项实测值的简单相加（8,787,816 − 32,768 = 8,755,048，实际 8,759,144）
：P2-2 第一步与 `-Oz` 叠加后边界节重新对齐，少了 4,096 字节的合成收益。

### 6.1 P1-1 为什么实测为零

lld 在 minicronet 这条链接命令行下**默认已经是紧凑布局**，2.1 节假设的 2 MB 对齐
padding 不存在：

- 默认（不加参数）：`.rodata` 结束于文件偏移 `0x2a9c7c`，`.text` 紧接着从 `0x2a9c7c`
  开始，两段之间没有文件 padding；产物 8,787,816 字节，`align = 0x1000`。
- 显式 `-Wl,-z,noseparate-code`：与默认**逐字节相同**（md5 一致、program header 完全
  一样），8,787,816 字节。
- 显式 `-Wl,-z,separate-code`：`.text` 被推到 `0x2aa000`，8,791,912 字节（+4,096）。

`align` 已经是 `0x1000`，所以提案里的 `-z max-page-size=4096` 是恒等操作。保留该参数
的唯一作用是钉住当前布局，防止未来 lld 改默认值时悄悄退回 padding；它今天既不省字节
也不改变 W^X 粒度（产物与不加参数时逐字节相同）。真正要紧的是把这条实测写进 commit
message，避免下一个人重新假设那里有 padding。

### 6.2 P2-1 为什么只省 0 字节

`InitializeInProcessPerfettoBackend()` 里的 `TrackEvent::Register()` 不是
`kCategoryRegistry` 的存活原因。每个 `TRACE_EVENT` 调用点都会实例化
`perfetto::internal::TrackEvent<&base::perfetto_track_event::internal::kCategoryRegistry>`
（见 `third_party/perfetto/include/perfetto/tracing/internal/track_event_macros.h` 的
`PERFETTO_INTERNAL_TRACK_EVENT_WITH_METHOD` → `TrackEvent::CallIfCategoryEnabled`），
而 base/net 中这些调用点自己就是活的，所以 registry、`kCategories` 一起活着。实测早
返回后体积 8,738,664 → 8,738,664（0 字节），`llvm-nm` 里 `kCategories`、
`kCategoryRegistry`、`TrackEventDataSource` 全部还在——未通过「符号归零」这条验收，
改动已回退（`base/BUILD.gn`、`base/trace_event/trace_event_impl.cc` 均恢复原状）。
要真拿到那 167 KB，只能让 `TRACE_EVENT` 在 `MINICRONET_BUILD` 下展开成空操作，
那是另一个量级的改动，超出本方案范围。

### 6.3 P2-2 第二步为什么没做

`third_party/icu/source/common/uts46.cpp` 直接调用 `u_charDirection()` 与
`ubidi_getJoiningType()`：前者读 `uchar_props_data.h` 的 `propsTrie`/`propsVectors`，
后者读 `ubidi_props_data.h` 的 `ubidi_props_trieIndex`。这三个表**就是** IDNA 语义的
一部分，与 2.2 节「不能直接删」的判断一致。用采纳了第一步的构建做了实测探针（8 个
断言，含 `例え.テスト` → `xn--r8jz45g.xn--zckzah`）：LTR 用例 8/8 通过，而
`bidi-ltr-then-hebrew`、`bidi-arabic-digit` 两个必须被拒绝的输入也如预期被拒——bidi/
joining 规则确实在生效，删表就是删这部分语义。结论：不实施，因此没有产生需要还原的
改动。

### 6.4 P2-3 未执行

产品侧要求保留 BoringSSL 的错误原因串（`ERR_reason_error_string()` 的文本，即
`kOpenSSLReasonStringData`）。改动已完整回退：`crypto/err/err.cc` 从未修改，
`gen/crypto/err_data.cc` 与 `third_party/boringssl/BUILD.gn` 恢复原状，两个仓的
`git status` 对这几个文件为空。该表的实测价值是 16,384 字节（−0.187%），做法（把
生成表在 `MINICRONET_BUILD` 下换成空表，`kOpenSSLReasonValuesLen = 0` 让
`err_string_lookup` 安全落空）已验证可行，将来若要启用可以直接复现。

### 6.5 采纳项的验证记录（固定顺序）

1. 重链 + strip：9,045,864 → **8,759,144** 字节。
2. `minicronet_smoke`、`minicronet_abi_cpp_smoke`、`run-http-smoke.py`
   （request timeout / request cancel）全部通过。`minicronet_websocket_smoke` 需要
   `argc == 3` 的在线端，与本次改动无关（仍是 `MINICRONET_WS_URL` 缺口）。
3. `cargo test --workspace`：8 passed / 0 failed。
4. Python 套件：103 用例 OK（2 skip，均为缺 WS 端点）。
5. IDN 探针：8/8 PASS，含 UTS #46 规范用例与两个必须拒绝的 `xn--` 输入。
6. 指纹红线：`tools/inspect-client-hello.py --repeat 4`，`chrome_99 / chrome_120 /
   chrome_140 / chrome_153` 四档采样。基线与本地产物的报告**逐字段相同**：稳定扩展
   15/15/15/16 个、变化扩展 0、chrome_153 的 28 条 trust anchor 一致，profile 间
   差异集合不变。`-Oz` 只改代码生成，没有触及 ClientHello 构造。
7. `-Oz` 基准：`tools/bench-core-baseline.py` 同口径 **8 轮 ×2**。同步吞吐 /
   asyncio 32 / asyncio 128 / 流式 64 MiB 四项，baseline 与 `-Oz` 的取值区间
   **全部重叠**（中位数分别 −0.7% / +3.9% / −0.3% / −3.7%）；慢消费者隔离 16 次
   全部 20/20 完成、0 失败。满足 2.2 节「区间重叠才采纳」的门槛。

### 6.6 收尾状态

1. ~~其余 7 平台未重建~~ → **已完成**，8 平台全部重建并安装（见 6.8）。
2. ~~审计上限未收紧~~ → **已完成**：三档 `MAX_BYTES` 已按「新体积 + 2%」下调
   （linux 9,050,000 → **8,940,000**；windows 11,400,000 → **10,400,000**；
   macos 8,750,000 → **8,620,000**），并在新上限下复跑 8 平台审计全部通过。
3. **仍未实现**：4.2 节新增的两项防膨胀机制（`audit-core-linux.sh` 的段级漂移断言、
   profile 表增量门禁）。
4. **真机验收**：macOS / Windows / ARM64 三个平台**没有可运行的目标机**，所以
   `-Oz` 与 P1-3 的性能回退在这些平台上只能靠真机验收，见 6.8 的风险说明。

### 6.7 8 平台编译配置复核（重建前）

结论：**八个 `tools/build-core-*.sh` 的 args.gn 已经逐行对齐，没有漏配的开关可捡。**
复核过的项与证据：

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| args.gn 一致性 | 已一致 | 8 份仅 macOS 多 `use_system_xcode` / `mac_sdk_*` 与 `enable_dsyms`，其余逐行相同 |
| `-Oz` 覆盖面 | 全平台生效 | 改在 `minicronet-core.patch` 的 `build/config/compiler/BUILD.gn`，与 `target_os` 无关 |
| ThinLTO 后端档位 | 已是体积最优档 | 链接命令里的 `-Wl,--lto-O0` 来自 `thinlto_optimize_default`；只有 opt-in `thinlto_optimize_max` 才用 `--lto-O2`，而 Chromium 自己的注释写明它会「substantially increase link time **and binary size**」（换来速度），并说「多数平台默认关闭就是因为空间开销太大」。这是要速度不要体积时才动的开关 |
| unwind 表 | 已排除 | `exclude_unwind_tables` 默认 = `is_official_build && !is_android`；编译命令里确有 `-fno-unwind-tables -fno-asynchronous-unwind-tables` |
| Windows DLL | 已最优 | `llvm-nm` 读到 0 个符号（link.exe 不为 release DLL 产出 COFF 符号表），`llvm-strip` 是空操作。节表 `.pdata` 227,328（x64 SEH 必需）、`.reloc` 31,744（ASLR 必需）、`.fptable`/`malloc_hook` 各 512 |
| Linux `.so` | 已最优 | ELF 的动态符号表 `.dynsym` 与 `.symtab` 分离，所以 `--strip-unneeded` 安全；8,787,816 已是 strip 后的数字 |
| macOS dylib | **不要加 strip** | 见下方陷阱，实测无正收益 |
| ICU 属性表（215 KB） | 不可行 | 6.3 节已实测 |
| ICU 转换机 `ucnv_*`（21,488 字节 / 45 符号） | 不值得 | 是 `Content-Type` charset 解码路径在用，且量太小 |

#### macOS 的 strip 陷阱（记下来防止重犯）

Mach-O 只有一张 `LC_SYMTAB`，本地符号与全局符号共用，不像 ELF 有独立的 `.dynsym`：

| `llvm-strip` 模式 | 体积变化（x86_64 / arm64） | `llvm-nm -gU`（审计依赖） |
| --- | --- | --- |
| 不加 | — | 20 ✓ |
| `-x`（只删本地符号） | **±0 / ±0** | 20 ✓ |
| `--strip-debug` | **±0 / ±0** | 20 ✓ |
| `--strip-all` / `--strip-unneeded` | −1,664 / −1,696 | **0 ✗** |

`--strip-all` 省的那 1.7 KB 就是把整张符号表删掉换来的，`audit-core-macos.sh` 的
`nm -gU` 二十符号检查会立刻失败。macOS 侧没有可捡的收益，**不要**加 strip 步骤。

#### 复核后仅剩的三个候选（都是取舍，不是漏配）

| 项 | 量级（linux-x86_64） | 性质 |
| --- | --- | --- |
| HSTS 预载表（P0-1） | **753,232（8.3%）** | 安全回退 + 与真实 Chrome 的可观测行为差异 |
| `use_partition_alloc_as_malloc=false`（连带关 BRP） | PA `partition_alloc::` 321,291 + `allocator_shim::` 15,268 ≈ **336,559（3.7%）是上限**，实际能省多少必须实测 | 改分配器行为 + 丢掉 BackupRefPtr 的 UAF 缓解；PA 本体被 base 深度依赖，不一定能整体去掉 |
| `use_safe_libcxx=false` | 未测（会从 `_LIBCPP_HARDENING_MODE_EXTENSIVE` 变 `NONE`） | 丢掉 libc++ 的边界/生命周期断言，安全类取舍 |

这三项都不属于「编译文件写错或漏写」，需要产品与安全拍板，因此本次重建不纳入。

### 6.8 八平台重建结果与 P1-3（Windows 尺寸档）

8 个平台于 2026-09-17 13:43–17:12 串行重建（不能并行，10 GB 内存），全部通过
`audit-core-*.sh`、`audit-abi.sh`、`audit-core-binaries.sh`，产物已由
`tools/install-core-binaries.py --abi-version 8` 安装并刷新 manifest。

| 平台 | 重建后 | 基线 | Δ | % |
| --- | --- | --- | --- | --- |
| linux-x86 | 8,578,368 | 8,641,720 | −63,352 | −0.73% |
| linux-x86_64 | 8,759,144 | 9,045,864 | −286,720 | −3.17% |
| linux-arm64 | 8,503,072 | 8,578,904 | −75,832 | −0.88% |
| windows-x86 | 7,939,584 | 9,058,816 | **−1,119,232** | **−12.36%** |
| windows-x86_64 | 10,188,288 | 11,391,488 | **−1,203,200** | **−10.56%** |
| windows-arm64 | 8,768,000 | 9,398,272 | −630,272 | −6.71% |
| macos-x86_64 | 8,442,400 | 8,712,800 | −270,400 | −3.10% |
| macos-arm64 | 7,690,768 | 7,872,464 | −181,696 | −2.31% |
| **合计** | **68,869,624** | 72,700,328 | **−3,830,704** | **−5.27%** |

#### P1-3：Windows 是唯一没吃到 `optimize_for_size` 的平台（计划外发现）

查「为什么 Windows 重建后逐字节没变」时定位到的。`build/config/compiler/BUILD.gn` 的
`config("optimize")` 第一个分支就是 `if (is_win)`，**在 `optimize_for_size` 判断之前**，
所以三个 Windows 目标一直在 `/O2,/clang:-O2`（速度档），Rust 也是 `-Copt-level=3`。
这解释了 Windows 为什么是最大的三个产物（占 8 平台总量 41%）。

改动（`minicronet-core.patch`，与 `-Oz` 同一文件）：

```gn
  if (is_win) {
    if (is_minicronet_build) {
      cflags = [ "/O1", "/clang:-Oz" ] + common_optimize_on_cflags
      rustflags = [ "-Copt-level=s" ]
    } else {
      cflags = [ "/O2", "/clang:-O2" ] + common_optimize_on_cflags
      rustflags = [ "-Copt-level=3" ]
    }
  } else if (optimize_for_size || is_chromeos) {
```

windows-x86_64 单独实测（`/O2` → `/O1 + -Oz`）：

| 节 | `/O2` | `/O1 + -Oz` | Δ |
| --- | --- | --- | --- |
| `.text` | 8,409,088 | 7,092,224 | **−1,316,864（−15.7%）** |
| `.pdata` | 227,328 | 359,936 | **+132,608** |
| `.rdata` | 2,638,848 | 2,619,392 | −19,456 |
| `.reloc` | 31,744 | 32,256 | +512 |
| 合计 | 11,390,464 | 10,187,264 | **−1,203,200** |

`.text` 掉 15.7%，代价是 `.pdata` 涨 132 KB（`-Oz` 生成的函数更多更碎，每个都要一条
SEH unwind 记录），净赚 1.2 MB。三个 Windows 目标合计 **−2,952,704 字节**，是一次
重建里最大的一笔。

**风险（必须记下）**：这是 `-O2 → -Oz` 的大跨度调整，比其余五平台的 `-Os → -Oz` 激进
得多（`.text` 少 15.7%），而且 Windows 产物**本机跑不了**，4.1 节那套「基准区间重叠才
采纳」的验收对它无效。Linux 五平台的 `-Oz` 已经过 8×2 轮基准验证，Windows 的这份只能
留待真机验收；如真机出现回退，回退成本就是这三行。

#### 另一个已记录的结论：`-Oz` 的收益是平台相关的

linux-x86 只降 0.73%（`.text` −43,887），而 linux-x86_64 降 2.85%（`.text` −274,330）。
原因：32 位目标默认不开 SSE，本来就没多少向量化/循环展开可关，`-Oz` 相对 `-Os` 已接近
饱和。不要再按统一的「−1~3%」预期套所有平台。


