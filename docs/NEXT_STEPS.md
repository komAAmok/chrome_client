# 后续工作清单

本文件是 `chrome_client` 在 `main` 分支上的执行顺序。它描述
“已存在的文件”与“已验证的能力”之间的区别；没有通过对应命令或真实目标机
测试的项目不能标记为完成。

## 约束

- `libminicronet` 是唯一的 TLS、HTTP/1.1、HTTP/2、HTTP/3/QUIC、
  WebSocket/WSS 和 HTTP/HTTPS/SOCKS4/SOCKS5 实现。
- Rust 只负责稳定 C ABI、所有权、回调、Future/Stream、取消、超时和错误映射。
- Python、Go、Node.js 只能做薄绑定，不得复制任何网络协议实现。
- Android、Java、公共 JNI、Chrome UI、Mojo、Variations 和无关 Chromium
  target 不进入发布物。
- 随机值、会话 ID、GREASE、ClientHello/QUIC padding、WebSocket key 等必须
  继续由 Chromium/BoringSSL/QUICHE 的 CSPRNG 生成，发布配置不得注入固定种子。
- 版本 profile 只覆盖已审计的 Chrome 99--155 差异；无法证明的行为必须
  `fail-closed`，不能用当前 Chromium 行为冒充历史版本。

## 阶段 0：Core 重建链（已完成）

**结论**：九个平台已全部重建成功，产物带 `minicronet/0.2.11` 版本串。
这一节原本断言「产物不可从零重建」，那个结论是错的，错在哪儿写在下面。

### 更正：gn gen 一直可以跑通，缺的是 pkg-config

原判断的推理链是：跑 `gn gen` 撞上 `//ui/gtk/BUILD.gn:16 assert(use_gio)`，
于是认定 `//ui` 的平台假设与 `is_minicronet_build` 冲突，不可调和。
实际是两处误判：

**一、手动跑 gn gen 少了 `--root-target`。** ninja 触发重生成时用的是

```text
../../buildtools/linux64/gn --root=../.. -q --fail-on-unused-args --regeneration \
  --root-pattern="//minicronet:*" --root-target=//minicronet gen .
```

只解析 Core 目标及其依赖，根本走不到 `//ui/gtk`。我手动跑的是不带参数的全量 gen，
那才会碰到 `gn_all` 底下的测试目标。

**二、真正的错误在更前面，我停在了第一处。** 带 `--root-target` 跑，报的是：

```text
FileNotFoundError: [Errno 2] No such file or directory: 'pkg-config'
See //build/config/linux/nss/BUILD.gn:11:3: whence pkg_config was called.
```

本机没装 pkg-config，而 `//net` 通过 `//build/config/linux/nss` 需要它。
`docs/BUILD.md` 里早就写了这个坑和解法（解包三个 deb 到 `/tmp/pkgconf`），
我没往下读到那里就下了结论。

### 重建九个平台实际需要什么

| 依赖 | 用途 | 获取方式 |
| --- | --- | --- |
| pkgconf 1.8.1 | Linux 目标解析 NSS（`//build/config/linux/nss/BUILD.gn:11`） | `apt-get download pkgconf pkgconf-bin libpkgconf3` 后 `dpkg-deb -x` 到 `/tmp/pkgconf/root` |
| ripgrep 14.1.0 | Windows 脚本的 Android/Java/JNI 泄漏检查 | `apt-get download ripgrep` 后解包，`PATH` 加进去 |
| xwin-winsysroot | Windows 三目标的 MSVC 头文件与库 | 放在 `$CHROMIUM_TOOLCHAIN/xwin-winsysroot`（约 1.5 GB） |
| Chromium 检出 + 已生成的 `build.ninja` | 全部九个 | `CHROMIUM_SRC`；缺 `build.ninja` 时用上面的 gn 命令生成 |

完整流程（`docs/BUILD.md` 有更详细的版本）：

```bash
export PATH=/tmp/rgpkg/root/usr/bin:/tmp/pkgconf/root/usr/bin:$PATH
export LD_LIBRARY_PATH=/tmp/pkgconf/root/usr/lib/x86_64-linux-gnu
export CHROMIUM_SRC=$HOME/chromium/src
export CHROMIUM_TOOLCHAIN=$HOME/chromium
export XWIN_ROOT=$CHROMIUM_TOOLCHAIN/xwin-winsysroot

tools/sync-core.sh                       # 应用 15 个补丁 + 写版本头

# Linux 四个：ninja 后必须 strip
for t in linux-x86 linux-x86_64 linux-armv7 linux-arm64; do
  ninja -C $CHROMIUM_SRC/out/MiniCronet-$t libminicronet.so
  $CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-objcopy \
    --strip-unneeded $CHROMIUM_SRC/out/MiniCronet-$t/libminicronet.so
done

# macOS 两个：脚本自带 strip
for t in macos-x86_64 macos-arm64; do
  ninja -C $CHROMIUM_SRC/out/MiniCronet-$t libminicronet.dylib
done

# Windows 三个：脚本自带 gn gen、编译与审计
for t in x86 x86_64 arm64; do tools/build-core-windows-$t.sh; done

python3 tools/install-core-binaries.py --abi-version 8 --chromium-src $CHROMIUM_SRC
tools/audit-core-binaries.sh
python3 tools/audit-core-payloads.py
python3 tools/record-core-build-args.py --check
```

### 0.2.15 的实测结果

九个平台全部用 0.2.15 重建，`CHROMIUM_REVISION` 与 ABI 12 都不变：

| target | 大小 | ABI |
| --- | --- | --- |
| linux-x86_64 | 8,662,576 | 12 |
| linux-x86 | 8,261,520 | 12 |
| linux-arm64 | 8,305,952 | 12 |
| linux-armv7 | 6,249,328 | 12 |
| macos-x86_64 | 8,135,728 | 12 |
| macos-arm64 | 7,476,608 | 12 |
| windows-x86_64 | 9,626,624 | 12 |
| windows-x86 | 7,664,128 | 12 |
| windows-arm64 | 8,324,096 | 12 |

本轮源码变化是 `kLastProfileMajor` 从 157 改为 158，加上 profile 表新增一条。
相对 0.2.14 的产物大小增量（实测，非估算）：

| target | 0.2.14 | 0.2.15 | delta |
| --- | --- | --- | --- |
| linux-x86_64 | 8,658,480 | 8,662,576 | +4,096 |
| linux-x86 | 8,260,920 | 8,261,520 | +600 |
| linux-arm64 | 8,304,608 | 8,305,952 | +1,344 |
| linux-armv7 | 6,248,592 | 6,249,328 | +736 |
| macos-x86_64 | 8,135,728 | 8,135,728 | 0 |
| macos-arm64 | 7,476,608 | 7,476,608 | 0 |
| windows-x86_64 | 9,626,112 | 9,626,624 | +512 |
| windows-x86 | 7,664,128 | 7,664,128 | 0 |
| windows-arm64 | 8,323,584 | 8,324,096 | +512 |

三个目标大小增量为 0 但确实重建过：它们的 sha256 都变了
（macos-arm64 `4fe50df4`→`66157efd`、macos-x86_64 `1291977c`→`f4cd0123`、
windows-x86 `adb52954`→`cbdcb235`），版本串从 0.2.14 变成 0.2.15，只是新增内容
落进了已有的段填充里，没有推动文件长度。这些数字取自各自 `manifest.json`，不是估算。

`chrome_158` 走完了三级证据链（`--levels 1,2,3` 全 PASS），验收记录见
[`CHROME_158_SUPPORT.md`](CHROME_158_SUPPORT.md)。

### 0.2.14 的实测结果

九个平台全部用 0.2.14 重建，`CHROMIUM_REVISION` 与 ABI 12 都不变：

| target | 大小 | ABI |
| --- | --- | --- |
| linux-x86_64 | 8,658,480 | 12 |
| linux-x86 | 8,260,920 | 12 |
| linux-arm64 | 8,304,608 | 12 |
| linux-armv7 | 6,248,592 | 12 |
| macos-x86_64 | 8,135,728 | 12 |
| macos-arm64 | 7,476,608 | 12 |
| windows-x86_64 | 9,626,112 | 12 |
| windows-x86 | 7,664,128 | 12 |
| windows-arm64 | 8,323,584 | 12 |

本轮唯一的源码变化是 `core/source/minicronet.cc` 对两个 DNS 字段的拒绝
（`dns_over_tls`、`dns_padding`），产物大小与 0.2.13 逐字节相同，只有版本串和
hash 变了——这是预期结果：拒绝发生在一个已有的 `if` 链里，没有新增代码路径。

### 0.2.13 的实测结果

九个平台全部用 0.2.13 重建，`CHROMIUM_REVISION` 不变，ABI 从 11 升到 12：

| target | 大小 | ABI |
| --- | --- | --- |
| linux-x86_64 | 8,658,480 | 12 |
| linux-x86 | 8,260,920 | 12 |
| linux-arm64 | 8,304,608 | 12 |
| linux-armv7 | 6,248,592 | 12 |
| macos-x86_64 | 8,135,728 | 12 |
| macos-arm64 | 7,476,608 | 12 |
| windows-x86_64 | 9,626,112 | 12 |
| windows-x86 | 7,664,128 | 12 |
| windows-arm64 | 8,323,584 | 12 |

`tools/audit-core-binaries.sh` 逐个核对版本串为 `minicronet/0.2.13` 与 32 个导出符号；
`audit-core-size-ceilings.py` 三个平台族的余量分别 2.05% / 1.97% / 1.98%；
`audit-core-payloads.py` 27 组 payload 逐字节一致；`record-core-build-args.py --check`
九个快照全部匹配；`audit-core-source-fingerprint.py` 报告 9 个产物均由当前 22 个
`core/source/` 文件构建。

**重建前必须先修 `core/source/smoke.c`**：它的 `on_redirect` 还是 ABI 11 的 8 参数签名，
v12 加了两个参数后 `minicronet_smoke` 编译失败，`build-core-linux-x86_64.sh` 在链接前就
停了。也就是说 ABI 升级后「能不能重建」这件事本身没有任何门禁守着——`audit-core-*`
读的是已提交的产物，`cargo test` 读的是 Rust 层，两者都不会去编 Chromium 侧的那个
smoke 程序。本轮把它修好并重建，但**这个缺口仍在**：下次动 ABI 时同样的失败还会先出现在
构建脚本里。

构建机的三个环境变量（本轮实测）：`CHROMIUM_TOOLCHAIN` 必须指向 `/home/sj/chromium`
（不是 `/home/sj/chromium/toolchains`），`XWIN_ROOT` 指向
`$CHROMIUM_TOOLCHAIN/xwin-winsysroot`，`OSXCROSS_TARGET` 指向
`$CHROMIUM_TOOLCHAIN/toolchains/osxcross-target`。默认值按 `$HOME` 推导，本会话的
`HOME` 是 `/home/sj`，但 `resolve_chromium_toolchain` 期望的是 Chromium 检出**同级**目录，
两者不一致时第一次交叉构建会把路径解析成 `/chromium/...` 并报「xwin winsysroot is
incomplete」，而真正的错误是根目录少了一层。

### 0.2.11 的实测结果

| 平台 | 产物 | 大小 |
| --- | --- | --- |
| linux-x86 | `libminicronet.so` | 8,256,760 |
| linux-x86_64 | `libminicronet.so` | 8,654,384 |
| linux-armv7 | `libminicronet.so` | 6,245,872 |
| linux-arm64 | `libminicronet.so` | 8,301,280 |
| macos-x86_64 | `libminicronet.dylib` | 8,131,568 |
| macos-arm64 | `libminicronet.dylib` | 7,476,544 |
| windows-x86 | `minicronet.dll` | 7,660,032 |
| windows-x86_64 | `minicronet.dll` | 9,622,016 |
| windows-arm64 | `minicronet.dll` | 8,319,488 |

九个产物 `strings` 逐个核对，版本串都是 `minicronet/0.2.11`，ABI 9 不变。

**0.2.11 的重建还修掉了一个门禁缺陷**：`tools/audit-core-linux.sh` 与
`tools/audit-core-windows.sh` 各自手写了一份导出符号表，ABI v9 只更新了
`audit-core-binaries.sh` 与 `audit-core-macos.sh`，于是这两份还停在 v8 的 20 个符号，
对任何正确的 v9 产物都会失败——v9 的 Linux/Windows 产物因此从未被这两个脚本审计过。
现在三处都改为读 `core/exports/minicronet.exports`（唯一声明处），符号数也从列表推导
而不是写死「twenty」。变异测试：从 exports 删一个符号、加一个不存在的符号，两个方向都
以 exit 1 失败。

### 配置快照

`tools/record-core-build-args.py` 从**构建输出**反推配置，而不是抄 `args.gn`。
依据是生成的 buildflag 头（`gen/base/debug/debugging_buildflags.h`、
`gen/base/cfi_buildflags.h`）——那些常量是真正到达编译器的，属于证据而非意图。
产出 `core/build-args/<target>.snapshot.txt`，文件头写明
「THIS IS A HISTORICAL RECORD, NOT A RECIPE」。

`--check` 已接入 AGENTS §5：改一个 flag、改 target_cpu、删一行，三种变异都非零退出。

### 仍然做不到的

- **32 位 ABI 布局没有运行时验证。** `cargo test --target i686-unknown-linux-gnu` 在链接阶段失败，
  缺 i386 的 `-lm` / `-lc` / `crtn.o`。静态布局审计（`audit-abi-layout.py`）
  通过，但那只是头文件与 Rust 声明的比对，不是真实执行。
- **七个平台无法在本机加载运行。** 只有 linux-x86_64 能真实发请求；其余八个的验证
  依靠 `audit-core-payloads.py`（payload 字节一致）与各平台的静态审计。

### 两条已否决的路径，留作记录

当初为解决「gn gen 失败」试过两条，都失败——但失败的原因是上面那个误判，
不是方法本身有问题。它们仍然不该做，理由不同：

| 路径 | 当时的结果 | 现在怎么看 |
| --- | --- | --- |
| A：补 `use_gtk` / `use_dbus` + 给 `//ui/gl` 写补丁 | 断言链继续走到下一环 | **不必做**：带上 `--root-target` 后 `//ui` 根本不在解析范围内 |
| A2：把 `gn_all` 置空 | 补丁生效但 `//ui/gl` 仍被解析 | **不必做**：同上。且改 `gn_all` 会影响所有非 minicronet 构建 |

### 已确认无效的优化方向，不要再试

三轮排查覆盖了 size optimization 的全部常见手段。**先看这一节，能省掉重复调查。**

| 方向 | 结论 |
| --- | --- |
| `--gc-sections` | **已在用**。不能用 `grep toolchain.ninja` 查（会误判为未启用），必须用 `ninja -t commands libminicronet.so` |
| `--icf=all` | **已在用**，且是 `all` 不是 `safe`。同上，查法一样 |
| `-Wl,-O2`（链接期） | **已在用** |
| `-flto=thin` + `-Wl,--lto-O1` | **已在用**。O2 测过更大，见下 |
| `-fwhole-program-vtables` | **已在用** |
| `-Oz`（编译期） | **已在用** |
| `-fno-unique-section-names` / `-z,noseparate-code` | **已在用**，省段名与段对齐 padding |
| `strip` | **已在做，且是最大一项**：未 strip 13,294,128 → `llvm-objcopy --strip-unneeded` 后 8,368,280，省 4.9 MB（37%） |
| 关 `dcheck_always_on` | **零收益**：`-DNDEBUG` 已生效，产物里 `Check failed` 只剩 1 条 |
| `-ffile-prefix-map` 抹平路径字符串 | **不可行**：见下，字符串来自 Rust rlib 不是 C++ |
| 裁 UMA 直方图名字 | **不可行**：无编译期开关，需改 Chromium 源码 |
| 减 profile 数量 / 关 QUIC / 关 WebSocket | **不该做**：放弃功能，与「保证现有功能」冲突 |
| 裁剪 `chrome_root_store` | **不该做**：347 KB，实测 249 张证书 / 331 KB DER，是 Chrome 的证书信任链本体。换系统 CA 库会让不同机器的校验结果不同 |
| 裁剪 Brotli 静态字典 | **不该做**：~112 KB，是 `br` 解码的必需数据；72 个 profile 的 `Accept-Encoding` 都含 `br` |
| 裁剪「协议字面量 / 错误消息」 | **不可行**：1.8 MB 全是 NSS/BoringSSL 错误串、net error 描述、QUIC/H2 帧名与字符查找表，都被代码引用 |

**为什么查法很重要**：我第一次用 `grep -o "gc-sections" toolchain.ninja` 得到零命中，
据此判定「未启用」，进而建议去做这项优化。实际上链接命令在 `libminicronet.so.rsp` 与
ninja 的 solink 规则里，`toolchain.ninja` 只存编译器与工具链定义。
**判据必须来自真实命令**：

```bash
cd $CHROMIUM_SRC/out/MiniCronet-linux-x86_64
../../third_party/ninja/ninja -t commands libminicronet.so | tail -1 | tr " " "\n" | grep -E "gc-sections|icf|lto-O"
```

#### 路径字符串：来自 Rust rlib，`-ffile-prefix-map` 无效

产物里有 388 条以 `../../` 开头的字符串，合计 22,564 字节。表面上看像是 C++ 的
`__FILE__`，但扫描全部对象文件后否定了这个猜测：

| 检查 | 结果 |
| --- | --- |
| C++ `.o` 里的路径字符串（扫 600 个对象） | **0 条** |
| Rust `.rlib` 里的 | **261 条** |

来源分布：`libcompiler_builtins` 160 条、`libcore` 70 条、`libstd` 15 条、
`liballoc` 11 条、`liblibc` 5 条。样例：

```text
../../third_party/rust-toolchain/lib/rustlib/src/rust/library/compiler-builtins/
  compiler-builtins/src/math/../../../libm/src/math/support/float_traits.rs
```

这些是 **Rust 标准库 panic 时的 Location 路径**，在编译工具链时固化进 `.rlib`。
改 C++ 编译 flag 不可能影响预编译好的 rlib；要修只能用 `rustc --remap-path-prefix` 重建整个 Rust std。
**收益 22 KB，代价是重建工具链——不值得。**

C++ 侧其实已经处理干净了：编译命令里有 `-g0` 与 `-ffile-compilation-dir=.`。

#### UMA 直方图名字：确认是 UMA 名，但没有编译期开关

641 条以 `Net.` / `Http.` / `Cookie.` / `Quic.` 开头的字符串，合计 26,729 字节。
抽样确认确实是 UMA 直方图名：

```text
Cookie.CookiePartitionSizeKibibytes
Cookie.CrossSiteRedirectDowngradeChangesInclusion2.Read
Cookie.GetCookieListWithOptions.Duration.Subsampled
```

但 `base/metrics/histogram_functions.h:80` 的实现是把名字作为**函数参数**传入：

```cpp
void UmaHistogramEnumeration(const StringType& name, T sample) {
```

没有 `ENABLE_UMA` / `UMA_ENABLED` 之类的编译期开关能把它变成空操作（`grep ENABLE_UMA` 在
`base/metrics/` 里零结果）。唯一的办法是改 Chromium 源码把 `UmaHistogram*` 改成空实现——
那是第 5 个 net 层补丁，**收益 26 KB，代价是又一处与上游分叉**。

#### `.rodata` 的完整分解（每一项都有实测依据）

| 项目 | 大小 | 性质 | 可精简 |
| --- | --- | --- | --- |
| Chrome Root Store 证书 | **347 KB** | 证书信任链本体 | ❌ 换系统 CA 库会破坏「与真正 Chrome 一致」 |
| Brotli 静态字典 | **~112 KB** | `br` 解码的必需数据 | ❌ 删了 `Accept-Encoding: br` 立刻失效 |
| UMA 直方图名字 | 26.7 KB | 641 条，见上 | ❌ 无编译期开关 |
| Rust 路径字符串 | 22.6 KB | 388 条，见上 | ❌ 需重建 Rust std |
| profile 表（数组 + 72 条记录） | ~10 KB | 已去重：1 份 cipher_suites 服务 72 个 profile | ❌ 上限只有 10 KB |
| 填充 / 测试模式 | 2.9 KB | 库内部常量表 | ❌ 可忽略 |
| hex 常量 | 1.1 KB | 协议查找表 | ❌ |
| 其余 ~1.8 MB | | NSS/BoringSSL 错误串、net error 描述、QUIC/H2 帧名、字符查找表 | ❌ 被代码引用 |

两项大头的实测方法：

- **root store 347 KB**：在产物里按严格 DER 规则扫描（外层 `SEQUENCE` 长度 500–2500，
  内层也是 `SEQUENCE` 且长度合理），得到 **249 张证书、331,047 字节 DER**，
  起始偏移 1,807,616、连续跨度 346,916 字节、相邻间隔中位数仅 9 字节——是一整块连续数据。
  用源文件交叉验证：`net/data/ssl/chrome_root_store/additional.certs` 441,237 字节 PEM，
  换算 DER 约 330,928，与实测差 119 字节。
- **Brotli 字典 ~112 KB**：最长一批「散文」字符串是 `firstvideolightworldmedia...`、
  `gustomentemariofirmacosto...` 这类，正是 Brotli 标准静态字典的内容；特征片段位于偏移
  2,567,680，在 `.rodata` 内连续。Chromium 侧对应 `third_party/brotli/enc/static_init.c:24`
  的 `BrotliGetDictionary()`。

#### 结论：8.37 MB 里没有「无用部分」可剔

剩余构成：

```text
.text     5.2 MB   真正被引用到的 Chromium net + BoringSSL + QUICHE 代码
.rodata   2.3 MB   见上面的完整分解表（无一项可剔）
其它      0.9 MB   重定位、动态符号、段表
```

这份产物已经是 `-Oz` + ThinLTO(lto-O1) + `--gc-sections` + `--icf=all` +
`-fwhole-program-vtables` + `-fno-unique-section-names` + `--strip-unneeded` 的组合结果，
属于 Chromium 能给出的接近极限的尺寸。继续挤压只能靠放弃功能。

### 接受 C 之后，静态门禁能守住什么（已实测）

三档变异，每次改完都**同步更新 manifest**，模拟「诚实但过期」的构建：

| 变异 | `audit-core-binaries` | `audit-core-payloads` | `size-ceilings` |
| --- | --- | --- | --- |
| 改产物任意字节，不改 manifest | 抓到 | 通过 | 通过 |
| 改产物任意字节 + manifest 同步 | **通过（漏）** | **通过（漏）** | 通过 |
| 改 payload 单字节 + manifest 同步 | **通过（漏）** | 抓到 | 通过 |

第三档的做法：从 `profile_table_generated.h` 取出 `trust_anchor_ids_0`（204 字节），
在产物里定位到偏移 907952，改动 payload 内第 10 个字节，重算 sha256 写回 manifest。
这是 C 场景下**最坏也最真实**的情况：有人重建了产物、诚实更新了 manifest，但 profile 表是旧的。

**所以 C 的代价是具体的：**

- 只有 **profile 数据**被静态守住：`audit-core-payloads.py` 从生成头取数组，要求每个平台
  的产物逐字节包含它们。这条链是有效的，第三档变异证明了它。
- **代码逻辑没有静态保护。** 第二档变异说明：改任意字节 + 同步 manifest，两个门禁都通过。
  如果 `minicronet.cc` 的逻辑改了却按旧 args 增量构建，没有静态门禁能发现。
- 非 linux-x86_64 的八个平台在 C 下**完全没有运行时验证**——只有 linux-x86_64 能真实加载并跑
  `run-http-smoke.py` 与 Python 套件。

**因此 C 可接受，但它守的是数据不是代码。** 这条必须写进文档，不能笼统说「接受限制」。

### 结论

体积这条路已到边际：`strip` 37% + LTO O1 0.65%，没有剩余空间。
「可复现」是真问题，但它需要的是**记录配置**，不是继续修断言链——A 与 A2 已实测否决，
继续深挖等于逐个 patch 掉 `//ui` 层的平台假设。

**采用 C**：接受「只能增量构建」，把它写成已知限制，并配以两条硬性要求：

1. 阶段 0 的三项任务（配置快照 / 限制声明 / 重建要求）必须完成；
2. `audit-core-payloads.py` 必须留在门禁清单里——它是 C 之下唯一能抓住
   「诚实但过期」构建的检查（见上表第三档）。删掉它，C 就变成裸奔。

详见 `docs/LESSONS.md` 第 48 条。

## 阶段 1-6 的勾选状态怎么读

这些清单写于项目启动时，勾选状态反映的是**写作时的计划**，不是当前实况。
本次逐条核对了一遍，把已经成立但未勾的项标为 `[x]` 并注明证据来源；
剩下的 `[ ]` 都是**仍未完成**的项。

核对时遵循的判据：**能指出具体的门禁、脚本或实测输出，才算完成**。
「代码里看起来有这个能力」不算——例如 Python 绑定确实在用，必须有真实请求的输出；
CI 确实存在，必须指出它跑了哪些审计。

一条容易误判的：阶段 1 的「运行 `cargo build --workspace --target <target>`」
现在本机装了九个 target 的 `rust-std`，但 32 位仍是失败的（缺 i386 的
`-lm` / `-lc` / `crtn.o`），所以这一项保持未勾。

## 阶段 1：多平台 Rust target 与 linker

- [ ] 添加本地可覆盖的 `.cargo/config.toml`；绝对路径只能由环境变量或 wrapper
  提供，不能写成唯一的机器路径。
- [ ] 为每个 target 建立独立 linker wrapper，并在 wrapper 中固定目标三元组、
  sysroot/SDK 和 Core import library：

  | 目标 | Rust target | 工具链边界 |
  | --- | --- | --- |
  | Linux x86 | `i686-unknown-linux-gnu` | Chromium i386 sysroot |
  | Linux x86_64 | `x86_64-unknown-linux-gnu` | 本机 glibc/NSS 或匹配发布 sysroot |
  | Linux ARMv7 | `armv7-unknown-linux-gnueabihf` | Chromium ARMhf sysroot (`install-sysroot.py --arch=arm`), hard-float, 需要 NEON |
  | Linux ARM64 | `aarch64-unknown-linux-gnu` | Chromium ARM64 sysroot |
  | Windows x86 | `i686-pc-windows-msvc` | xwin Windows SDK/CRT + `lld-link` |
  | Windows x86_64 | `x86_64-pc-windows-msvc` | xwin Windows SDK/CRT + `lld-link` |
  | Windows ARM64 | `aarch64-pc-windows-msvc` | xwin Windows SDK/CRT + `lld-link` |
  | macOS x86_64 | `x86_64-apple-darwin` | osxcross + macOS SDK |
  | macOS ARM64 | `aarch64-apple-darwin` | osxcross + macOS SDK |

- [ ] 运行 `cargo build --workspace --target <target>`，不能只以安装了 Rust
  std target 或 `cargo check` 作为完成依据。
- [ ] Linux x86/ARMv7/ARM64 做 ELF 架构、解释器、sysroot 和动态依赖审计；不能用
  x86_64 glibc 冒充其他架构。
- [ ] Windows 做 PE 架构、导出符号、import library、`icudtl.dat` 配对审计。
- [ ] macOS 做 Mach-O 架构、最低系统版本、install name 和系统框架审计。
- [ ] 目标机或对应 GitHub runner 完成真实 H1/H2/H3、WS/WSS、代理、取消和
  TLS 验证；交叉编译成功不等于运行时成功。

## 已知缺陷：进程正常退出时偶发 SIGABRT（退出期静态析构竞态）

**现象**：跑 `AsyncSession` + `asyncio.gather` 三个批次（每批 60 并发）后，
**程序已经打印完所有结果**，在解释器 teardown 阶段约 1.7%（1/60）概率收到
`Fatal Python error: Aborted`，退出码 134（SIGABRT）。`faulthandler` 只能看到一条
没有 Python 帧的线程，说明 abort 发生在原生侧。

**复现**（无需本仓库以外的依赖）：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  bash -c 'for i in $(seq 1 80); do
    python3 /tmp/abort_threads.py >/tmp/o.txt 2>&1 || { echo "abort at $i"; break; }; done'
```

**已做的排除，都带证据**：

- **不是本轮 GIL/detach 改动引入的**。用 `git show HEAD:bindings/python/src/lib.rs`
  重建未改动的二进制，60 次里同样 abort 1 次。
- **不是探测脚本的缺陷**。探测在 abort 前已把采样 JSON 写进 stdout，
  `leak-probe` 才报 `status: error` 而不是 `fail`——它读到的是"没有结果"，而实际是
  "有结果但进程没干净退出"。
- **发生在正常退出路径上**。同一程序结尾改 `os._exit(0)`：100 次 0 abort；
  结尾走正常 `return`：60 次 1 abort。同一 child 用 `os._exit` 30 次 0 abort。
- **gdb 下不复现**。8 个并行 gdb worker 各 30 轮（共 240 轮）一次都没抓到——插桩改变了
  时序，这也正是竞态的特征。

**当前状态（更新，ABI 12 / 0.2.13）**：`leak-hunt` 的 `async-gather` 形态在本轮
复现了 **1/20（5%）**，且**全部采样都已打印完成之后**才收到 `SIGABRT`（返回码 -6，
`stdout` 完整）。这与本节开头记录的现象是同一个：进程退出期，结果已经算完。

用 ABI 11 的产物测 200 次曾是 signal=0，所以这条记录现在的准确表述是：**它仍然存在，
概率在 1/20 量级，与 Core 的 ABI 版本无关**——本轮唯一变化是重建了产物，而
`docs/review-0.2.13/summary.json` 里的 `leak-hunt` 是重跑一次拿到 13/13 flat 的结果。

因此 `review-before-tag.py` 的 `leak-hunt` 步骤有约 5% 概率报 `12/13 shapes flat`，
**重跑即可**；判据没有放宽，也没有把这个形态从清单里删掉——那会让它变成没人看的缺口。

期间还发生一次假警报，值得记下：我曾跑出「120/120 abort」，原因是复现脚本
`/tmp/abort_threads.py` 已被清理，`python3` 报 `can't open file` 并返回非零，
而循环只判断「非零即失败」。**计数正确、判据错误**——与本文档前面记的两次
归因错误是同一类。之后的测量都改成按信号（`returncode < 0`）判定。

**已做的一件事：让这个缺陷可被自动识别**。`tools/leak-probe.py` 之前把子进程
被信号杀死和子进程抛异常都记成 `status: error` + 一行 stderr，`SIGABRT` 完全
看不出来；而且子进程的 stdout 是块缓冲，进程在退出期被信号杀掉时**已算好的采样
会整段丢失**，父进程只看到一个信号、零输出。现在：

- `returncode < 0` 时按 `signal.Signals` 报到名字（`signal: "SIGABRT"`）；
- 若 stdout 里已有采样，reason 写 `child died with SIGABRT after printing its samples`，
  并把最后一行采样放进 `stdout_tail`；
- 子进程打印采样后显式 `flush`，信号杀进程也丢不掉。

三档行为实测（真实 child + 末尾注入 abort）：干净退出 → `pass`；信号退出且无输出
→ `child died with SIGABRT`；信号退出且已打印 → `child died with SIGABRT after
printing its samples`，`stdout_tail` 保留采样。

**尚未定位**：`libminicronet.so` 的静态析构顺序与进程级 Chromium 单例
（`base::ThreadPoolInstance`、`net::NetworkChangeNotifier`、`base::i18n`）之间的顺序
假设。`Runtime` 本身是 `new` 出来的、永不释放（`core/source/engine.cc:360-362`），
所以 `~Runtime` 里的 `AtExitManager` 不参与，真正跑的是 Chromium 自己的静态析构。

**为什么现在不修**：定位它需要带符号的 Core（`is_debug`/`symbol_level`）或 ASan 构建，
两者都要重跑 Chromium 的完整链接；而且修法在 Core，不在绑定层。在此之前，
`leak-probe` 的 `async-gather` 形态有约 1.7% 概率误报，重跑即可，但**必须知道它是误报**。

## 阶段 2：Core 与 ABI 发布闭合

- [x] 以 `core/abi/minicronet.h` 为唯一 ABI 来源，对照 `core/abi/ABI_VERSION`、
  `crates/minicronet-sys/src/lib.rs` 的 FFI 常量与声明，以及 `core/exports/` 下的
  `minicronet.def`/`minicronet.exports`/`minicronet.lds` 三份导出表，做逐字段、
  版本、尺寸、对齐、导出符号审计。
  -> 已由 `tools/audit-abi.sh`（28 符号 × 4 载体，外加 `ABI_VERSION` 与 Rust
     常量两处版本比对）与 `tools/audit-abi-layout.py`（结构体尺寸/对齐）覆盖，
     两者都在 AGENTS §5 门禁里。
  -> `ABI_VERSION` 曾经停在 8 而头文件已经是 10，因为当时没有任何门禁读它；现在
     `audit-abi.sh` 比对它和 Rust 常量，各自改成 9 都会非零退出（变异测试已做）。
- [ ] 对 9 个 `core/binaries/<target>/manifest.json` 重新计算 SHA-256、大小、
  Chromium revision、ABI 和运行时依赖。
  （`tools/install-core-binaries.py` 会写 sha256 与 size_bytes；当前九个 manifest 一致，
  但这一项依赖重建，见阶段 0 与下文「产物来源证明」。）
- [ ] Linux 保持架构匹配的 NSS/NSPR 与 glibc 动态依赖；不静态链接 glibc/NSS，
  不把其他架构的库打包进来。
- [ ] Windows 保证 DLL、`.lib` 与 `icudtl.dat` 同架构同版本。
- [x] 运行 `tools/audit-core-binaries.sh`，并保存每个平台的审计结果。
  -> 已是 AGENTS §5 门禁，当前 9 个目标全通过。
- [x] 发布包不得包含 Chromium 源码、GN/Ninja 输出、`.o`、静态归档、Siso
  缓存、smoke 可执行文件、Android/Java/JNI 文件或本地证书私钥。
  -> `.gitignore` + `tools/audit-core-inputs.sh`（检查 25 个 BUILD.gn 名、22 个安装文件、
     15 个补丁全部被跟踪）。`bindings/*/target/` 与 `target/` 从不提交。

## 阶段 3：Rust 安全层审计与冻结

- [x] 核对 `minicronet-sys` 与 ABI v8：结构体、枚举、回调签名、`size/version`
  前置字段和所有导出函数一一对应。
  -> `tools/audit-abi.sh` + `tools/audit-abi-layout.py`；32 位分支仍无执行路径，
     见下文「32 位 ABI 布局没有执行路径」。
- [ ] 验证 Engine、Request、ResponseStream、WebSocket 的创建、重复 release、
  异步回调、Engine 关闭、跨线程 `Send/Sync` 和 panic 边界。
- [ ] 验证固定上传、分块上传、自动/手动/错误重定向、缓存模式、Cookie、请求头、
  超时、取消、网络错误、32 并发。
- [ ] 验证直连、HTTP/HTTPS 代理、SOCKS4/5、WS/WSS 以及 H2/H3 Extended CONNECT。
- [x] 只保留 `futures-core` 这类最小接口依赖；Rust 不引入第二套网络栈。
  -> `crates/minicronet/Cargo.toml` 的 `[dependencies]` 只有 `futures-core` 与
     `minicronet-sys`（path），比这一项要求的还严。
- [x] 完成 `cargo fmt --check`、`cargo check --workspace --all-targets`、
  `cargo clippy --workspace --all-targets -- -D warnings` 和离线单元测试后冻结
  公共 Rust API。
  -> 四条都在 AGENTS §5，本机已实测全绿（Rust 1.99.1，见上文「工具链：本机已就绪」）。

## 阶段 4：Chrome profile 与隔离

- 历史证据和规范化参数已迁入 `profiles/`（`normalized_profiles.json`、
  `effective_protocol_params.json`、`network_feature_snapshots.json`，以及每个发布
  版本一个证据目录 `profiles/chrome_152/`、`profiles/chrome_153/`）；运行时只编译
  精简只读表，不把源码证据文本编入二进制。
- [x] 验证 `chrome_86`--`chrome_158` 的 TLS/ALPN、H2 SETTINGS、QUIC/H3、WS
  和关键 FeatureList 差异；未验证版本保持拒绝或明确降级状态。
  -> 73 个 profile 全部有三级证据；`audit-tls-version-range.py`、`audit-h1-alps-absence.py`、
     `audit-h3-profile-support.py`、`audit-padding-rule.py` 四个门禁守着，86-92 的 h3
     标为不支持并 fail-closed（实测 chrome_86 + v3 → MN_ERROR_PROFILE_UNSUPPORTED）。
- [ ] 确认 ProfileContext 在 Engine 创建时冻结，profile namespace 进入连接池、
  TLS/QUIC session、Alt-Svc、HTTP cache、Cookie 和代理复用键。
- [ ] 同一进程创建多个 profile Engine，确认无进程级可变 FeatureList 或缓存污染。
- [ ] 三次独立 ClientHello/QUIC/WS 连接检查随机性，不比较需要随机变化的字段的
  单次哈希相等。

## 阶段 5：语言绑定

- [x] 先冻结 `Rust API`，再实现 Python、Go、Node.js 薄绑定。
  -> Python 两个 wheel（cp36-abi3 / cp39-abi3）真实可用；`tools/audit-python36-artifact.py`
     验证旧扩展能驱动共享 facade。Go / Node 目录只有设计说明，未实现——这一点在
     `bindings/go/README.md`、`bindings/node/README.md` 里如实声明。
- [x] 绑定只做字符串/字节/回调/错误类型转换，不实现 TLS、HTTP、QUIC、WS 或代理。
  -> 本轮审查发现并修掉了三处违反：cookie 退役污染服务端路径、WS 握手绕过 store、
     Python 侧重发重定向。详见 `4d12f07`。
- [x] 每个绑定至少有一次真实 Linux x86_64 smoke，并检查 Engine/Request 生命周期。
  -> Python：346 个用例（含 `test_process_pool` 这个环境项）在本机真实发请求；
     `tools/audit-python36-artifact.py` 另跑一次 legacy 扩展的真实请求。
     Go / Node 尚未实现，见阶段 5 第一项。
- [ ] 为其他平台提供加载路径、Core 选择和运行时依赖说明；不在绑定层复制 Core。

## 阶段 6：CI、打包与发布

- [x] CI 分离 Rust 结构检查、Core 二进制审计、各平台交叉编译和目标机运行测试。
  -> `.github/workflows/ci.yml`（check + python-regression 两个 job，跑 5 项 core 审计）与
     `build-wheels.yml`（Python wheel 矩阵）。目标机运行测试仍未覆盖，见阶段 1。
- [ ] 免费 GitHub runner 只承担可复现构建/静态审计；需要真实运行的架构使用对应
  runner 或目标机，不把交叉编译当作运行时验收。
- [ ] 生成每个 target 的可追溯 manifest、符号表摘要、依赖清单和测试报告。
- [ ] 只发布 `core/abi`、对应 Core/依赖、Rust crate 和薄绑定；排除证据抓包、缓存、
  私钥、临时 CA、构建目录和开发工具缓存。
- [ ] 在打 tag 前重新执行全矩阵并记录 Chromium revision；升级 revision 必须重新
  生成 profile、重新构建和重新抓包审计。

## 当前可执行顺序

```text
0. 锁定构建配置快照 + 声明「不可从零重建」（阶段 0；A/A2 已否决）
1. target/linker wrapper + sysroot/SDK 检查
2. 九个 target 的 Rust build 与产物审计
3. ABI/Core manifest 重新核对
4. Rust 生命周期与网络回归
5. profile/隔离回归
6. Python/Go/Node 薄绑定
7. CI 和发布包验收
```

性能相关的两项 ABI 改动不在上面这条链里，它们各自是一次独立的 ABI 版本推进：
「合并首包等待」可先做（只增不改、风险集中在一处），「批量唤醒投递」要等它的观测
数据稳定后再动。详见本文「提案：两项性能 ABI 改动」。

### ABI 13 计划：三项头部校验下沉（已排期）

这是 AGENTS §1.1 迁移的**收尾**，也是 `tools/audit-layer-ownership.py` 里仅剩的三条
`KNOWN`。它们不是新功能：绑定层今天用三张 frozenset 做 Core 本该做的事，只因为 Core
还不具备拒绝能力。详细论证与逐条改动见本文末尾「提案：三项头部校验下沉」。

| 序 | 改动 | 绑定层要删的表 | Core 侧落点 |
| --- | --- | --- | --- |
| 1 | 头名拒绝返回 `MN_ERROR_INVALID_ARGUMENT` | `_CORE_OWNED_HEADERS`（7 名） | `Request::StartOnNetworkThread` 头部循环之前 |
| 2 | 按名删除入口（或"这个名字不可删"的查询） | 同上表里删除分支的名单 | 合并集构建处 `MergeFrom` 旁 |
| 3 | WebSocket 保留名拒绝 | `_FORBIDDEN_WS_HEADERS` | `mn_websocket_config_t` 校验 |

**为什么必须一轮做完**：三项都要动 `MN_ABI_VERSION`。分三次就是三次版本升级、三次
九平台重建、三次绑定层同步；而且它们共享同一个契约问题——"哪些头名不可写"必须只有
一处定义，否则头名拒绝与删除报告会各写一份名单，重演今天绑定层的问题。

**顺序不可颠倒**：先让 Core 拒绝，再删绑定层表。反过来会让一次失败同时暴露两个原因。

**与 ABI 12 的关系**：ABI 12（结构化重定向头 + 跨域 Authorization 剥离 + 零拷贝接管）
已经落地并九平台重建完成，是本计划的**前置基线**。三项下沉在它之上叠加，不需要回退任何
v12 能力。

**完成判据**（与提案一节一致，此处复述以便单点核对）：

- `KNOWN` 里这三条被删除，且 `audit-layer-ownership.py` 仍通过
- 绑定层三个 frozenset 消失，相关测试同步更新
- 九平台重建并重新记录 build-args 快照与 manifest
- `docs/COMPATIBILITY_BOUNDARY.md` 与 README 写明"这些头名现在由 Core 拒绝"

**风险**：三项都是**收紧**——今天被静默接受的调用会开始报错。这是 §8 fail-closed 要的
方向，但属于对外行为变更，必须写进文档，且头名集合要与 Chromium 实际行为一致
（判据是 `net/spdy/spdy_http_utils.cc:214-221` 与 `SetExtraRequestHeaders` 的真实语义，
不是照抄 RFC 列表）。

## 打 tag 前必须跑 review-before-tag.py（§13）

`AGENTS.md` §13 现在要求每次打 tag 前跑一次全量 review，产出的 `summary.json`、
`bench.json`、`leak.json` 与 tag 一起提交。

`review-before-tag.py` 里有两步依赖本机之外的条件，跑不了时报告会写成
`unavailable` 并给出原因，这不是通过也不是失败：

| 步骤 | 需要什么 | 没有时会看到 |
| --- | --- | --- |
| `profile-addition` | 网络（googlesource）+ 该 profile 的 tag 源码 | `unavailable`，原因写明缺网络 |
| `profile-probes` | Chromium 检出 + gn/ninja | 同上 |
| `wire-smoke` | `out/MiniCronet-linux-x86_64/minicronet_smoke` | 原因里给出构建该二进制的脚本名 |

第一次跑（0.2.9）时 28 步全部 pass，耗时 233 秒；`profile-addition` 用的是
`--profile chrome_157 --levels 1,2`，与 AGENTS §5 里那行 CI 口径一致。如果要全表
扫一遍（72 个 profile × 9 个文件），那是几千次请求，单独跑：

```bash
python3 tools/audit-profile-addition.py --all --levels 1
```

## 待办：linux-armv7 没有一次真实运行

> **2026-11 进展（仍未完成）**：`qemu-user-static` 已在不使用 `sudo` 的前提下取得
> （`apt-get download` + `dpkg-deb -x` 到 `/tmp/qemu`），`qemu-arm --version` 输出
> `8.2.2`，armhf sysroot 里 `libc.so.6`、`ld-linux-armhf.so.3`、`libnss3.so` 都在。
> 但运行现成的 ARM 二进制直接段错误：
>
> ```
> qemu-arm-static -L <armhf sysroot> ./minicronet_abi_cpp_smoke
> # qemu: uncaught target signal 11 (Segmentation fault) - core dumped
> ```
>
> 用 sysroot 里的 `ld-linux-armhf.so.3` 显式启动结果相同，而
> `qemu-arm-static /usr/bin/true` 报 `Invalid ELF image for this architecture`，
> 说明 qemu 本身工作正常、问题在 ARM 侧。**尚未区分**是 qemu 对这份 Chromium 产物的
> 兼容问题，还是产物本身有问题：armhf sysroot 里没有链接器（`ld`），无法编一个最小
> ARM 程序把两种情况分开。
>
> 因此状态**没有变化**，仍是「已构建、静态审计通过、未加载、未发请求」。在这一点上
> 发布 ARMv7 wheel 仍然不符合 AGENTS §13。

`core/binaries/linux-armv7/` 已构建、已审计、已提交，但本机没有 `qemu-user`
（`apt-cache policy qemu-user-static` 有候选包，`sudo` 需要密码），也没有 ARMv7
硬件，所以这个平台和另外 7 个一样停在「已构建、静态审计通过、未加载、未发请求」。

补齐任一条即可把状态推到「能发请求」：

1. 在有 ARMv7 的机器（树莓派 3/4 的 32 位系统，或 `arm32v7/python` 容器）上跑
   `LD_LIBRARY_PATH=core/binaries/linux-armv7 python3 tools/run-http-smoke.py`；
2. 在装了 `qemu-user-static` 的 x86_64 主机上跑同一条命令，并确认
   `qemu-arm -L <armhf sysroot>` 能找到 `libnss3.so`；
3. 在 CI 里加一个 `debian:bullseye-armhf` + `qemu` 的 job。

三种都要先装 armhf 的 NSS（容器内 `apt-get install libnss3 libnspr4`，或把
`debian_bullseye_armhf-sysroot/usr/lib/arm-linux-gnueabihf/` 加进 `LD_LIBRARY_PATH`）。
做完之前 README 与 `docs/PLATFORM_SUPPORT.md` 都只能写「已构建、已审计」。

## 待办：profile_state_isolation_probe 缺测试服务器

`core/source/profile_state_isolation_probe.cc` 断言两个 Engine 之间的 cache、cookie、
连接池与 TLS session cache 完全隔离，但它驱动 `/cache`、`/etag`、`/cookie-redirect`、
`/connection/*`、`/tls-close/*` 这些路径，需要一个专门的 HTTP + HTTPS 服务器。那个
服务器不在本仓库里，所以 `tools/audit-profile-probes.sh` 把它记为**显式跳过**
（打印 `SKIP ... needs HTTP_BASE and HTTPS_BASE`），不是静默跳过。

补法：把那个 harness 加进 `tools/`（一个自签 CA 加固定响应体的小服务即可），然后让
`audit-profile-probes.sh` 自己起它并把两个 base 传进去。在补上之前，这一格行为验证是
**未执行**状态，不要在任何文档里把它记成通过。

Python 资源生命周期、流式响应、WebSocket 有界队列、Core 回调隔离以及 ABI v8
pause/resume 的详细设计和验收门槛见
[`PYTHON_RESOURCE_BACKPRESSURE_PLAN.md`](PYTHON_RESOURCE_BACKPRESSURE_PLAN.md)。
当前阶段 1--3 已完成 ABI v8 实现；阶段 4 的每请求/每 WebSocket 独立顺序 callback
runner 已随 8 个平台 Core 重建上线；阶段 5 的 HTTP pause/resume 已落地为
`mn_request_resume_read` 加 `on_body` 返回值；WebSocket 侧则通过
`HasPendingDataFrames()` 加 `ResumeReading()` 实现消费者驱动 pause/resume
（未消费帧让 channel 暂停读取，消费完再恢复）。

Linux x86_64 的可复现构建、体积构成和吞吐/并发基线见
[`BASELINE_LINUX_X86_64.md`](BASELINE_LINUX_X86_64.md)。

## 工具链：本机已就绪（2026-11）

此前 `cargo` / `rustc` 都不在本机，导致 `cargo fmt` / `check` / `clippy` / `test` 四条门禁
从未真正跑过，`bindings/python36` 的 wheel 也无法重建。根因不是权限，而是**下载源**：

```
static.rust-lang.org   8 秒内 0 字节（官方源不可达）
mirrors.ustc.edu.cn    2.2 MB/s
rsproxy.cn             1.2 MB/s
```

换 `RUSTUP_DIST_SERVER=https://mirrors.ustc.edu.cn/rust-static` 后，整个 toolchain
（rustc + cargo + rust-std，宿主 target）**60 秒装完**；九个 target 的 `rust-std` 与
`rustfmt` / `clippy` 组件同样走镜像。crates 依赖用
`sparse+https://mirrors.ustc.edu.cn/crates.io-index/` 亦可拉通。

现在本机可执行：

| 门禁 | 结果 |
| --- | --- |
| `cargo fmt --all -- --check` | 通过 |
| `cargo check --workspace --all-targets` | 通过（13.9s） |
| `cargo clippy --workspace --all-targets -- -D warnings` | 通过 |
| `cargo test --workspace --all-targets` | 通过（2 个 abi_layout 用例） |
| `maturin build`（bindings/python36） | 通过，产出 cp36-abi3 wheel |

需要 `patchelf`（`pip install patchelf`）；缺它时 maturin 会在 repair 阶段失败并留下
一个 22 字节的空 wheel。

已安装的 target：`x86_64/i686/aarch64-unknown-linux-gnu`、
`armv7-unknown-linux-gnueabihf`、`i686/x86_64/aarch64-pc-windows-msvc`。
macOS 两个 target 的 `rust-std` 也装了，但交叉链接仍需要 osxcross 与对应 SDK。
## 已完成：bindings/python36 的产物门禁（0.2.9）

`bindings/python36/src/lib.rs` 里 `#[test]` 数量是 0，所以 `cargo test --manifest-path
bindings/python36/Cargo.toml --locked` 只会编译然后退出 0。它曾经是打 tag 前 review 的
`python36-tests` 一步——一个**不可能失败**的门禁，报告里却写着 Python 绑定已验证。

现在那一步换成 `tools/audit-python36-artifact.py`，检查的是产物本身：

1. 扩展能按自己的模块名导入（`PyInit_chrome_client_native36`）；
2. `abi_version` 等于从 `core/abi/minicronet.h` 读到的 `MN_ABI_VERSION`；
3. 没有链接 `libpython`，且除白名单外不引用 `_Py_*`（真的 abi3）；
4. 能驱动 `bindings/python` 的 facade 发一次真实请求并记录响应 cookie。

第 4 条是这次改动的重点：前三条都能被一个「能导入但已过时」的扩展满足，而那种扩展
只在 facade 调用它没有的方法时才暴露。

变异测试（AGENTS §6）：把 `MN_ABI_VERSION` 改成 9 → 门禁报
`reports abi_version=8 but ... declares 9`；把 `.so` 里的 `PyInit_` 名称改一个字节 →
门禁报 `dynamic module does not define module export function`。两条都非零退出，已恢复。

配套加了 `tools/build-python36-wheel.sh`：本地构建 wheel 的唯一入口，构建完顺带跑上述
门禁，并断言 wheel 标签必须是 `cp36-abi3`。

### 重建 wheel 时发现的缺陷：facade 只认一个模块名

wheel 建出来后装进干净 venv 实测，**3.12 上 import 直接失败**：

```
ImportError: chrome_client native extension 'chrome_client_native' is not installed
```

原因：两个 wheel 的 `requires-python` 都是 `>=3.6,<3.16`，所以 pip 可以把 cp36-abi3
那个装到任何受支持的版本上；而 facade 的 `MODULE_NAME` 只按 `sys.version_info` 选一个名字。
当装的是「另一个」wheel 时，它问的名字在包里根本不存在。

`tools/audit-pypi-metadata.sh:303` 的门禁注释其实早就写明这个后果——「3.x–3.y 会拿到错误
模块，在这个 README 标为支持的版本上 import 失败」——但门禁只比对 switch 点与 abi3 floor
是否相等，没有验证运行时真的能加载。

修法：`_native.py` 引入 `FALLBACK_MODULE_NAME`，`load()` 先按首选名字找，找不到再试另一个。
实测同一个 wheel 在 3.12 venv 里从「import 失败」变为「请求 200 + 收到 cookie」。

门禁同步扩展：`tools/audit-python36-artifact.py` 现在会解包 `dist/` 里最新的 wheel，用
**当前解释器** import 其中的 facade。变异测试：把 wheel 里的 fallback 去掉重打包 →
门禁报「the facade must accept either module name」；恢复后通过。
**本机未能完成的部分**：这台机器没有 Rust 工具链（`cargo`/`rustc` 均缺，`apt` 需要
sudo 密码，`rustup` 装到 3 个组件时下载停滞），所以 `maturin build` 没能真正跑通。
门禁与脚本都已在**现有的** `bindings/python36/target/release/libchrome_client_native36.so`
上验证（该产物 abi 8、无 libpython 依赖、能驱动 facade 完成真实请求）。重新产出一个
新 wheel 仍需在有工具链的机器上执行 `tools/build-python36-wheel.sh`。
## 提案：ABI v9 十项（cookie 存取 / 协商协议+ECH / client hints / DNS / host mapping / mTLS / 原生能力补齐 / 平台身份 / 非幂等重发 / hop-by-hop 头）

十项改动的完整提案见 [ABI_V9_PROPOSAL.md](ABI_V9_PROPOSAL.md)（cookie 存取、协商协议 + ECH 状态、
client hints、DNS 配置、host mapping、mTLS、四项原生能力补齐、平台身份、非幂等重发修复、
hop-by-hop 头）。本文件下面四条待办
（协商协议、client hints、DNS）都已并入那份提案，外加一项此前没有登记的 cookie 存取。

为什么不拆开做：八项 ABI 改动都要同步 `MN_ABI_VERSION`、三份导出表与九个平台产物，拆开等于多次重建；
第九项（非幂等重发）不改 ABI，可以单独出一个补丁版本，不必等 v9
等于重建九次。但**落地顺序**要拆：提案第 7 节建议先做协商协议（只加 getter、不改结构体
布局、32 位断言不用动），再做 cookie（改动最大但回滚边界清楚），DNS 次之，
client hints 最后（它阻塞在 72 份 profile 证据上，不应阻塞 v9 发布）。

第五项（host mapping）与第六项（mTLS）都是评估时补上的，不在最初的清单里：

- host mapping 关闭一个现存测试盲区（`ERR_NAME_NOT_RESOLVED` 在本机被 DNS 拦截，一直
  不可测），并且是 CDN 直连的前置条件；
- mTLS 让一整类双向认证部署（企业内网 / 金融网关 / IoT 管理）从"完全不可用"变为可用，
  且注入点是 public 的、不改变 ClientHello 指纹。

第十项（`Connection` 等 hop-by-hop 头）与第九项同类，都是「行为与声明不符」，但只需绑定层
`fail-closed`：`Connection: close` 现在能发出去却不关闭连接（实测 3 次请求仍共用 1 条），
而在 h2/h3 上它被 `spdy_http_utils.cc:214-221` 静默丢弃——同一份代码的行为随协商协议变化。
修法是把 RFC 9110 §7.6.1 的 hop-by-hop 集合整组拒绝，不加 ABI、不重建平台产物。

第九项（非幂等重发修复）与第十项都是**语义缺陷**，其余都是能力缺失或保真度不足。
实测：服务端在复用连接上直接关闭响应时，POST 被重发两次、body 也逐字节重发，而 `retry=0`
拦不住——重发发生在 Chromium 的 `HttpNetworkTransaction` 内部，判据只看「连接是否复用」
与「是否收到响应头」，不看方法是否幂等。逐层验证后确认不 patch 无法关闭，因此需要
第四个 `net/` 层补丁（`HttpUtil::IsMethodIdempotent` 已现成）。取舍：从「可能重复提交」
变成「可能因连接重置而失败」——失败可处理，重复提交不可撤销。

第八项（平台身份）来自两个实测发现：传参 `User-Agent` 后 `sec-ch-ua-platform` 会消失或
仍是本机值，三处平台身份可以互相矛盾；而 `impersonate=None` 的默认 UA 是 `minicronet/0.2.9`，
一眼看出不是浏览器。修法不是「自动修正传参 UA」（那会砍掉「伪装成 Windows」这个真实需求），
而是新增 `platform_override` 让三者由同一个值派生，同时把默认 UA 改成 Chrome 版本串。
顺带补一个现在完全缺失的门禁：UA / platform / mobile 的一致性从未被断言过。

第七项（原生能力补齐）来自一次系统审计：把 `URLRequestContextBuilder` 的 41 个 `set_*`
入口与 Core 实际用的 8 个做差集，剩下的四项按影响排序——HSTS 完全没启用（对 HSTS 站点
会先发明文 HTTP）、cookie 语义缺 `CookieAccessDelegate`（SameSite 落桶与 Chrome 不同）、
H2 session params 用 C++ 默认（END_STREAM 帧选择可能不同）、代理缺 delegate（多跳链）。
前三项里只有前两项进 ABI；H2 params 属 `RuntimeProfileData`，是数据问题不是接口问题。

ECH 只暴露状态（第 2.5 节）：桌面端 Chromium 的 `EchMode` 恒为 `kDisabled`，暴露配置
等于在 Core 里重建一条 Chrome 从不走的路径，所以只加一个 getter。

提案没有登记、也不应等 v9 的三项：`headers={"User-Agent": None}` 的删除语义、
`tools/audit-python-stubs.py` 的硬编码清单、`bindings/python36` 的空壳门禁与 `include` 路径。

## 提案：两项性能 ABI 改动（合并首包等待 / 批量唤醒投递）

这两项来自同步与异步两侧的压测结论（`docs/SYNC_CONCURRENCY.md`、
`docs/ASYNC_CONCURRENCY.md`），它们的共同点是：**瓶颈已经不在绑定层**。
绑定层能做的都做完了——cookie 路径、代理解析、netrc 展开、header 解析都已优化并
有回归测试；剩下的开销在 ABI 的调用形状里。

登记在这里是为了让它们可被检索、可被排期，而不是留在两份性能文档的末尾。
**两项都不改变现有语义**，只减少每请求的往返次数；都按 ABI v9 的方式同步
`MN_ABI_VERSION`、三份导出表与九个平台产物。

### 一、现状：每请求两次阻塞等待

同步路径实测（`docs/SYNC_CONCURRENCY.md` §5.5，1 KiB 响应，300 请求）：

| 步骤 | p50 | 占请求 |
| --- | --- | --- |
| `start_stream`（等响应头） | 1.12 ms | **51%** |
| `read_body`（等 body） | 0.56 ms | **26%** |
| Python 侧全部 | 0.24 ms | 11% |

两次等待是 `mn_request_start` 之后 `PyRequest::start_stream` 与 `read_body` 各自
`Condvar::wait_timeout(WAIT_SLICE)` 一次。响应头与 body 在 Core 侧由同一个
`OnCompleted`/`OnReadCompleted` 序列驱动，绑定层却要醒来两次。

异步侧同理：`notify_scheduled` 是每请求一个 `AtomicBool`，每次 Core 事件唤醒一次
`call_soon_threadsafe`，实测每请求 1.7 次唤醒、`notify()` p50 18.7 us，
`notify` 之间的间隔总计 583 ms / 512 请求。

### 二、提案 1：一次调用取回首包 + 首块 body

```c
/* 新增，不改变既有符号语义 */
typedef struct mn_response_head {
  uint32_t size;            /* = sizeof(mn_response_head_t) */
  uint32_t version;         /* = MN_ABI_VERSION */
  int32_t  status_code;
  int32_t  http_version;
  size_t   header_bytes;
  const uint8_t *headers;   /* 仅本次调用期间有效 */
  size_t   body_bytes;      /* 首个 body 块，可能为 0 */
  const uint8_t *body;      /* 仅本次调用期间有效 */
  int32_t  body_complete;   /* 1 = 整个响应已结束 */
} mn_response_head_t;

MN_EXPORT mn_result_t MN_CALL
mn_request_await_head(mn_request_t *request, uint64_t timeout_ms,
                      mn_response_head_t *out_head);
```

语义：等价于「等到 `on_response` 与第一次 `on_body`（或完成）都到达」，
在 Core 的网络线程上一次装好，只唤醒等待线程一次。

**收益：已重测，24% 不再成立。** 那个数字是按 **2.1 ms p50** 的旧基线算的。ABI 11
的当前实测 p50 是 **521 µs**，在同一基线上把三次唤醒合并成一次，实测量到的是
**503.1 → 456.6 µs，即 9%**（`coalesced-drain` 实验，见 `docs/ABI.md`）。
差额是前几轮优化挣来的：GIL detach 与读 buffer 复用已经把这块大部分吃掉。

**结论：不做。** 9% 的顺序延迟，代价是一个新 ABI 符号、每个请求两个同步对象
（`WaitableEvent` + `Lock`）、一次首块堆分配，以及一条与 `on_body` 并存的
**双路径首块语义**——后者意味着任何一处状态竞争都会让首块丢失或重复，而这正是
本项目现有门禁（`leak-hunt`、`test_compat`）覆盖不到的一类缺陷。实测代码已写完
又整体撤回，撤回后 `audit-abi` 仍为 28 符号。

**代价与风险**：

- 需要 Core 记录「首块是否已随头一起到达」，`Request::StartOnNetworkThread` 里
  的 `OnResponseStarted` 回调要缓存首块。
- `body` 指针只在调用期间有效，与现有 `on_body` 的契约一致，不新增生命周期规则。
- 流式路径（`stream=True`）不受影响：它本来就不该等 body。
- 不改任何既有导出符号，旧调用方零改动。

### 五、`discard_cookies` 的重定向跳仍会带上本请求自己设的 cookie（登记缺陷）

`discard_cookies=True` 的 promise 是「这次请求不发送、不记录 cookie」。实测一个 302 链：
第一跳回 `Set-Cookie: hop=SENT`，**第二跳把 `hop=SENT` 发出去了**（服务端读到
`['hop=SENT']`）。会话 jar 事后仍然是空的，所以「不记录」成立，「不发送」只对第一跳成立。

原因在 Chromium 侧而不是实现：一次 `URLRequest` 跟随重定向时用的是同一个
`URLRequestContext`，`CookieMonster` 在第一跳之后已经接受了那条 cookie，第二跳由
`URLRequestHttpJob::AddCookieHeaderAndStart` 正常附加
（`net/url_request/url_request_http_job.cc:761`）。换引擎只能隔离「跨请求」，隔不开
「同一次请求的两个跳」。

要修只能在 Core 加 per-hop 的 cookie 抑制，而 M156 没有这个开关
（`docs/TBD_VERIFICATION.md` T-1 已证：`LOAD_DO_NOT_SEND_COOKIES` M87 删除，
`set_disallow_credentials` 连带改隐私模式与客户端证书）。**在此之前应当写进
`discard_cookies` 的 docstring 与 README 的已知限制**，而不是让它继续作为未文档化的
行为差异存在。改动前的那一版也是同样的行为（已用 HEAD 树实测），所以它不是本轮引入的。

---

### 四、`kCurrentChromiumMajor = 157` 与产物 M156 不一致（登记缺陷，不是疑问）

`core/source/minicronet.cc:102` 写的是 `157`，而 `core/binaries/*/libminicronet.*` 是从
`58bcc148f76`（= **M156.0.8078.31**，`chrome/VERSION` 与 `manifest.json` 都这么写）构建的。

**这是指纹面，不是文案问题**：`impersonate=None` 时 UA 由这个常量拼出来
（`core/source/minicronet.cc:149`），于是默认会话宣称 `Chrome/157.0.0.0`，而同一连接的
TLS/H2/H3 参数来自 M156。所有命名 profile 不受影响——它们从 profile 表拿 UA。

处置：Core 里那个常量必须由构建时读到的 `chrome/VERSION` 生成，或与 `CHROMIUM_REVISION`
一起校验；在此之前 `audit-profile-evidence.py` 已经比对 revision，但没有任何门禁比对这个
常量。它在本次审查的 diff 之外，登记在此而不是随这次改动一起动。

---

### 三、提案 2：批量唤醒投递

```c
/* 新增 */
typedef void(MN_CALL *mn_engine_wakeup_fn)(void *user_data,
                                           mn_request_t **requests,
                                           size_t count);

MN_EXPORT mn_result_t MN_CALL
mn_engine_set_wakeup_batch(mn_engine_t *engine,
                           mn_engine_wakeup_fn callback, void *user_data,
                           uint32_t max_batch);
```

语义：Engine 把多个请求的就绪事件攒到一个批次里投递，绑定层一次
`call_soon_threadsafe` 唤醒多个 `_AsyncState`。批次由 Core 侧的空闲判定驱动
（例如队列为空或达到 `max_batch` 时投递），不引入固定延迟。

**收益**：异步 512 并发下唤醒次数从约 1.7 次/请求降到接近 1 次/批。按实测
`notify` 总耗时 22.4 ms、间隔总计 583 ms 计，**循环停摆峰值有望从 105 ms 降到
数十毫秒量级**。这是目前异步侧唯一还没被消除的结构性开销。

**代价与风险**：

- `notify_scheduled` 的语义从「每请求一个」变成「每批次一个」，Rust 侧的去重逻辑
  要重写；这是**最大的一处风险**，需要单独设计而不是顺手改。
- 引入批次意味着单请求的唤醒可能被延迟到同批其它请求就绪，**必须实测尾部延迟**
  （p99/p999）是否退化，退化则不可接受。
- 批次投递失败（loop 已关闭）时要有降级路径，不能丢事件。

### 四、建议顺序与判据

| 顺序 | 项 | 前置条件 | 验收判据 |
| --- | --- | --- | --- |
| 1 | 提案 1（合并首包等待） | 无。不改既有符号 | 同步 p50 下降 ≥ 15%；`stalled_consumer_isolation` 不退步；五种泄漏形态仍 flat |
| 2 | 提案 2（批量唤醒） | 提案 1 落地并观测稳定 | 512 并发循环停摆峰值下降 ≥ 40%，**且 p99 不退化** |

提案 1 先做，因为它只增不改、收益确定、风险集中在 Core 一处。提案 2 的批次语义
会影响所有异步请求的唤醒时机，应当在提案 1 的观测数据稳定后再动。

### 五、明确不在本提案内的事

- **不**为了性能放宽任何 `fail-closed` 行为（`UnsupportedFeature` 的触发条件不变）。
- **不**引入固定延迟来「攒批」（那会直接抬高 p50）。
- **不**改变 `mn_request_callbacks_t` 的既有回调语义；新入口是并存的第二条路径。
- 绑定层内部那点剩余开销（`os.getpid()` 5.3 us 做 fork 检测、`engines.get` 7 us）
  **不值得**为此改 ABI：`os.register_at_fork` 能省掉 `getpid()`，但 `EngineCache` 是
  每会话实例，注册/注销回调与「缓存已销毁」的竞态要处理的复杂度远大于 5.3 us 的收益。
  这条写在这里是为了避免以后有人重复评估。

---

## 已定位：`unittest discover` 的两条失败记录都不成立，真正的问题是运行时长

第五轮把前四轮留下的两条「已知缺陷」查到底了。**两条都不是产品缺陷，也不是测试缺陷**，
而是我自己诊断时的环境错误。如实记录，避免下次再被同样的假信号带走。

### 1. `test_process_pool` 的 `BrokenPipeError` —— 我的 env 缺 `PATH`

现象：该测试反复 `BrokenPipeError`，`resource_tracker._fd` 是 `None`，看起来像
「导入 `chrome_client` 破坏了资源追踪器」。

**实际原因**：`multiprocessing` 的 `spawn` 子进程靠 `sys.executable` 重新启动解释器，
而我传给 `execFile` 的 `env` 是 `{ ...process.env, ... }` —— 这个沙箱里 `process.env`
是**空对象**，于是子进程拿到一个没有 `PATH`/`HOME` 的环境，`spawn` 起不来，父进程写管道
就得到 `BrokenPipeError`。追踪器 `_fd` 为 `None` 只是「还没成功启动」的后果，不是原因。

**判定实验**（同一脚本、同一目录、只改 env）：

| env | 结果 |
| --- | --- |
| `{}` | FAIL FAIL FAIL FAIL |
| `{PATH, HOME, LANG}` | OK OK OK OK |
| 真实测试 + 完整 env | OK OK OK OK OK |

**结论**：`test_process_pool` 是健康的。之前所有「5/5 失败」「稳定复现」都是我把空环境
传下去造成的。

### 2. `discover` 的「挂起」—— 不是挂起，是慢

368 个测试串行跑，八个模块分别运行合计约 130 秒，`discover` 一个进程里更久。它超过
工具单次调用上限后被 SIGKILL，看起来像挂起。

排查过程中我还犯了一个会自我印证的错误：用 `loadTestsFromNames` 写的诊断脚本**没有
`__main__` 保护**，而 `spawn` 子进程会重新执行 `__main__`，于是子进程又去建进程池——
那是我脚本的递归缺陷，不是仓库的问题。加上保护后，同一批测试、同一顺序全部通过
（352 个前缀 + 挂起测试）。

**结论**：`discover` 没有挂起；它只是比单次工具调用预算更长。八个模块分别运行全部通过，
`discover` 需要更长的运行窗口或拆分执行。

### 3. 留下的教训（写进本节而不是只写在脑子里）

1. 传 `env` 给子进程时，**必须显式构造完整环境**；这个沙箱的 `process.env` 是空的，
   `{...process.env}` 等于什么都不传。
2. 任何会 `spawn` 的诊断脚本都要有 `if __name__ == "__main__":`。
3. 「稳定复现」也可能是自己的错误稳定复现——**先换环境对照一次**，再下结论。

## 提案：三项头部校验下沉（头名拒绝 / 删除无效报告 / WS 保留名）

> **状态：已排期为 ABI 13**，见本文「当前可执行顺序」下的「ABI 13 计划」。
> 前置基线是已落地的 ABI 12。本节保留完整论证，作为实施时的判据来源。

**这三项是 AGENTS §1.1 迁移的剩余部分**，不是新功能。绑定层今天有三张表在做 Core
本该做的事，它们之所以还在，只有一个原因：**Core 还不具备相应的拒绝能力**。

按 §1.1 的主判据问一遍——"换 Go 或 Node 重写这个绑定，这三张表要不要再写一遍？"
答案是"要，而且必须逐字节一致，否则同一个请求在不同语言里行为不同"——所以它们属于
Core。`tools/audit-layer-ownership.py` 的 `KNOWN` 里登记的就是这三条。

### 现状与证据（都已在代码里复核过）

| 绑定层的表 | 位置 | Core 今天为什么不拦 |
| --- | --- | --- |
| `_HOP_BY_HOP_HEADERS` | `sessions.py:232` | `request.cc` 只做 `SetExtraRequestHeaders`，没有任何头名校验 |
| `_CORE_OWNED_HEADERS` | `sessions.py:211` | `minicronet.cc:772/829` 只校验 `header_count` 的范围，不按名字拒绝；`SetExtraRequestHeaders` 只增不减，删除是 no-op 且 Core 无法报告 |
| `_FORBIDDEN_WS_HEADERS` | `sessions.py:85` | `websocket.cc:265` 把 `headers_` 逐条 `SetHeader`，不检查保留名，与 Chromium 追加的 `Origin`/`Upgrade` 会冲突 |

### 三项改动

**1. 头名拒绝：`MN_ERROR_INVALID_ARGUMENT`**

`mn_request_create` 校验每个 `mn_header_t.name`：hop-by-hop 集合
（`connection` / `proxy-connection` / `keep-alive` / `te` / `trailer` / `upgrade`）出现即拒绝。
契约写进头文件，绑定层删表。

**2. 删除无效的报告**

`headers={"User-Agent": None}` 今天在绑定层被拦，因为 Core 侧是静默 no-op。
需要 ABI 能回答"这个名字我删不掉"，绑定层才能把拒绝的理由交给 Core 而不是自己维护名单。
两种实现：给 `mn_request_config_t` 加 `remove_header_count` / `remove_headers`，由 Core
返回失败；或在 `mn_request_create` 之外加一个查询入口，问某个名字是否 Core-owned。

**3. WS 保留名校验**

`mn_websocket_config_t` 校验 `Host` / `Origin` / `Upgrade` / `Connection` /
`Sec-WebSocket-*` 与 `User-Agent`，出现即拒绝。Core 在 `websocket.cc` 里已经知道这几类
由 Chromium 追加（那条注释就是证据），只是没有把判断变成拒绝。

### 为什么必须一轮做，而不是逐个叠

三项都要动 `MN_ABI_VERSION`：改校验语义、加结构体字段、加查询入口。分三次做就是三次
版本升级、三次九平台重建、三次绑定层同步。**而且它们共享同一个契约问题**——"哪些头名
是不可写的"必须在一处定义，否则头名拒绝和删除报告会各写一份名单，重演今天绑定层的问题。

### 代价与风险

- 三项都是**收紧**：今天被静默接受的调用会开始报错。这是 §8 fail-closed 要求的方向，
  但会改变对外行为，需要在 README 与 COMPATIBILITY_BOUNDARY 里写明。
- 头名拒绝必须与 Chromium 自己的行为一致，否则会拒绝掉 Chromium 本来能处理的头。
  判据是 `net/spdy/spdy_http_utils.cc:214-221`（h2/h3 丢弃的三个）与
  `SetExtraRequestHeaders` 的实际语义，不是凭 RFC 列表。
- 顺序：**先加 Core 拒绝，再删绑定层表**。反过来会让一次失败同时暴露两个问题。

### 完成判据

- `tools/audit-layer-ownership.py` 的 `KNOWN` 里这三条被删除，且门禁仍通过
  （该门禁的规则是"条目不再匹配任何代码即失败"，删表后自然满足）。
- 绑定层三个 frozenset 消失，改名/改集合的测试同步更新。
- 九平台重建（含 macOS；osxcross 一直都在，见 ABI v11 那节的平台状态）。
## 已完成：ABI v10（回调携带结构化头 + reason + ECH 查询）

v9 的回调签名 (`on_response(status, headers, len)`) 不足以让绑定层不做解析，v10 把它
扩成 `(status, reason, reason_len, fields, count, headers, len)`。

**版本必须升而不仅仅是改签名**：Core 在 `minicronet.cc:765` 校验
`callbacks.version == MN_ABI_VERSION`。签名变了但版本号不动，旧的 v9 绑定会通过校验然后
被 9 参数调用 5 参数函数。实测：v9 调用方现在得到 `MN_ERROR_UNSUPPORTED_ABI`(2)，
v10 调用方 `MN_OK`。

**顺带接通 ECH**：`mn_request_ech_status` 从 v9 起就在 Core 里，一直没接到绑定层。现在
四层贯通，`Response.ech_status` 报告 `success` / `rejected` / `not_attempted` / `None`。

**修正（本轮实测）**：当时把 `not_attempted` 当成桌面端的唯一可能值，这个说法不成立。
ECHConfigList 只来自 DNS HTTPS 记录，引擎在传了任一 dns_* 参数时会自己建 resolver 去查
（`core/source/engine.cc:801`），拿到就真加密。实测 chrome_158 对 `crypto.cloudflare.com`：
系统解析 `not_attempted`/sni 明文，`dns_nameservers="1.1.1.1"` 5/5 `success`/sni 加密。

### 平台状态：九个全部重建（ABI 11）

| 平台 | ABI | 说明 |
| --- | --- | --- |
| linux-x86 / x86_64 / arm64 / armv7 | **11** | 已重建并安装 |
| windows-x86 / x86_64 / arm64 | **11** | 已重建并安装 |
| macos-x86_64 / macos-arm64 | **11** | 已重建并安装 |

**更正**：本节此前写「七个已重建，两个没有」，并列出 macOS 缺失的四个组件。
那四条全部是误判——osxcross 在 `$CHROMIUM_SRC/../toolchains/osxcross-target`，
与 Chromium 检出同级。当时的搜索是 `find / -maxdepth 4 -name "osxcross*"` 且只搜了
一个文件系统，所以「没命中」被当成了「不存在」。详情与证据见 `docs/ABI.md` 的
「v11 的产物覆盖」一节。两个 macOS 目标随后各用约 14 分钟建成并自审通过。

## 已完成（ABI v9）：协商协议由 Core 报告## 已完成（ABI v9）：协商协议由 Core 报告

`mn_request_negotiated_protocol` 已随 ABI v9 落地，Core 从
`HttpResponseInfo::connection_info` 填写，经 `HttpConnectionInfoToCoarse` 分类，
因此 draft HTTP/2 与各 QUIC 版本都被覆盖（`core/source/request.cc` 的
`OnResponseStarted`）。四层全部接通：Core → `minicronet` 的
`Request::negotiated_protocol()` / `Response::http_version` → pyo3 的
`PyResponse.http_version` 与 `PyRequest.http_version()` → facade 的
`Response.http_version`。

**实测**：本地 h1 服务端上 `pin=None` 与 `pin=v1` 都报 `HTTP/1.1`；在此之前两者都
报 `None`，因为 facade 只从状态行推断。

两个诚实性要求仍然成立，且都由实现保证：

- `MN_HTTP_VERSION_UNKNOWN` 是 Core 自己的答案（命中 HTTP 缓存时如此），facade 如实
  报 `None`，不升级成 `HTTP/1.1`——头文件把这一点写成契约。
- 重定向中间跳没有 Core 响应可问，`_finalize` 保留状态行回退，而回退只在能证明时
  给出协议。

**仍未接通**：`mn_request_ech_status`（ECH 是否实际使用）还没有对应 getter。它与
协议同属 v9 新增的查询面，接法相同，是下一件可做的事。

## 已完成（facade 层）：默认请求头 = profile 抓包；Core 注入仍属 ABI v9

**现状（2026-11 落地）**：`impersonate=` 现在发出该 profile 自己的 h1 抓包头集合，
逐字节且顺序一致，72 个 profile 全部通过。表由 `tools/generate-client-hints.py` 从
`profiles/chrome_<major>/h1/captures.json` 生成，`tools/audit-client-hints.py` 每次
提交前重新推导；门禁见 AGENTS.md 第 5 节。平台跟随宿主（与 Core 的 UA 推导一致），
`Sec-Fetch-*` 取导航语义，两者都可被调用方按字段覆盖，`default_headers=False` 可整体关闭。

**仍未做的部分（ABI v9）**：让 Core 自己注入，而不是 facade 拼 extra headers。
facade 注入的代价是这些头不经过 `net` 层的 URLLoader 工厂，因此 Chromium 自己的
`Sec-Fetch` 策略与 client-hint 协商（响应头 `Accept-CH` 决定服务器想要哪些 hint）
都不作用于它们——一个只靠 `Accept-CH` 才该发 hint 的服务器，这里仍然每次都发。
下面的原始分析保留，作为 v9 的输入。

### 原始分析（v9 的背景）

> **以下为 2026-11 之前的观测，保留作历史记录。** 当时 `Session(impersonate="chrome_155")`
> 只发 `Host`、`Connection`、`User-Agent`、`Accept-Encoding` 四个头。facade 层已修，
> 见上一节。

**当时的现状（实测，非推断）**：`Session(impersonate="chrome_155")` 发出的请求头只有
`Host`、`Connection`、`User-Agent`、`Accept-Encoding` 四个；`impersonate` 只把
`User-Agent` 的版本串换成 `Chrome/155.0.0.0`，**默认头集合一个都不变**。

真实 Chrome 在同样的请求上还会发 `sec-ch-ua`、`sec-ch-ua-mobile`、
`sec-ch-ua-platform`、`Upgrade-Insecure-Requests`、`Accept`、`Sec-Fetch-Site`、
`Sec-Fetch-Mode`、`Sec-Fetch-User`、`Sec-Fetch-Dest`、`Priority` 和
`Accept-Language`。因此任何同时检查 `sec-ch-ua` 与 `User-Agent` 的检测方都能把本
客户端判定为非 Chrome。这是**未闭合的保真度缺口**，README 已如实声明。

**为什么现在拿不到**：那些头由 Chrome 的 `services/network` URLLoader 工厂层注入
（client hints 与 Sec-Fetch 源自 renderer 的请求头），不在 `net` 层。
`net::URLRequestContextBuilder` 只有 `set_user_agent` 与 `set_accept_language`
（`net/url_request/url_request_context_builder.h:244`），二者都落到
`StaticHttpUserAgentSettings`，而该接口只提供 `GetAcceptLanguage()` /
`GetUserAgent()`——没有产出 client hints 或 Sec-Fetch 的入口。仓库内零处
`user_agent_metadata` / `UserAgentMetadata`。

**当时的修法设想（ABI v9，仍是 Core 侧注入的正确做法）**：

1. Core 侧为每个 profile 携带 `UserAgentMetadata`（品牌列表、full version、
   platform、mobile 标志）与 UA 品牌 GREASE 顺序。Chrome 的 client hints 由
   `net::HttpUtil`/`ClientHints` 序列化，取值随版本变化，必须逐版本从官方源码取。
2. 在 `Request::StartOnNetworkThread`（`core/source/request.cc`）用
   `SetExtraRequestHeaders` 注入 `sec-ch-ua*`；`Sec-Fetch-*` 与 `Priority` 需要
   在 ABI 上新增字段表达（导航型 vs 子资源型请求的取值不同），不能硬编码一组。
3. `Accept` 与 `Accept-Language` 可先用现有 Engine 字段补齐，代价最小。
4. 必须先用 `tools/inspect-client-hello.py` 的同款 `--repeat` 采样法对**真实
   Chrome** 抓包，逐版本确认取值与**顺序**后再冻结。按 AGENTS.md §7，不得把
   当前猜测值写成期望值。

**验收（已由 facade 层实现）**：新增一个本地 echo 服务器用例，断言 `impersonate="chrome_155"` 时请求头
集合与真实 Chrome 155 抓包一致（含顺序），且 `impersonate=None` 时**不**注入任何
client hint——默认路径不能因为补 profile 而被污染。

## 待办：`not_attempted` 需要拆成「没查」与「查了但没有配置」

**现状**：`mn_ech_status_t` 只有三个值（`core/abi/minicronet.h` 的 `mn_ech_status` 枚举），
而 `MN_ECH_NOT_ATTEMPTED` 现在同时代表四种情形：

1. 没传 dns_* 参数，默认 resolver 根本不查 HTTPS 记录（`core/source/engine.cc:801` 分支未进入）；
2. 查了，域名没有 HTTPS 记录（`tls.peet.ws` 这类）；
3. DoH 拒绝后回退，解析结果里没有 HTTPS 记录（实测出现过一次）；
4. 有记录但 Chromium 侧 `ssl_info.encrypted_client_hello` 为 false。

调用方拿不到「我配了 DNS 但 ECH 没生效」的诊断——这与 `MN_HTTP_VERSION_UNKNOWN` 当初的问题同型。

**改法**：Core 在 `core/source/request.cc` 记录 `ssl_config_.ech_config_list` 是否为空，
追加 `MN_ECH_NO_CONFIG`（查过但没拿到配置）与 `MN_ECH_GREASE_ONLY`（发了 GREASE，未加密），
保留 `MN_ECH_NOT_ATTEMPTED` 表示「本轮没有查 DNS HTTPS 记录」。
这是**追加**枚举值，不动已有取值，所以 ABI 版本号只 +1，28 个导出符号不变；
需要同步 `crates/minicronet-sys` 的布局测试、`bindings/*` 的字符串映射与 `models.pyi`。

**完成判据**：四个平台各一条真实请求，分别落在四种取值上，并由 `bindings/python/tests` 断言；
同时 `README.md` 的 ECH 一行改写为按取值区分的表格，而不是现在一句「取决于是否自己查 DNS」。

## 待办：逐请求 / 会话级 DNS 配置（ABI v9）

**现状（实测，非推断）**：`dns_options` 在仓库内**零命中**——`grep -rn "dns_options"`
扫过 `*.py` / `*.pyi` / `*.rs` / `*.cc` / `*.h` / `*.md` 无任何结果，Python 绑定里连
`dns` 这个词都不存在。实测：

```
>>> chrome_client.get("http://example.com/", dns_options={"timeout": 1})
TypeError: Session.request() got an unexpected keyword argument 'dns_options'
>>> chrome_client.get("http://example.com/", doh_url="https://dns.example/dns-query")
UnsupportedFeature: doh_url= is not supported: ABI v8 exposes no Core setting for it
```

即最接近的 DNS 开关 `doh_url` 是被**主动拒绝**的（`sessions.py:995` 与 `:1444` 的
fail-closed 分支），不是已实现。根因在 ABI：`mn_engine_config_t` 的全部字段为
`user_agent` / `proxy_rules` / `proxy_username` / `proxy_password` /
`http_cache_mode` / `accept_language` / `profile_id` / `profile_namespace` /
`protocol_mode` / `tls_verify_mode` / `custom_ca_pem`，**没有任何 DNS / resolver /
DoH / nameserver 字段**，DNS 行为完全由 Chromium 内部默认值决定。

**Chromium 侧注入点已核实齐全**（这是可行性依据）：
`net/dns/public/dns_config_overrides.h` 提供 `DnsConfigOverrides`，字段为
`nameservers` / `dns_over_tls_active` / `dns_over_tls_hostname` / `search` /
`append_to_multi_label_name` / `ndots` / `fallback_period` / `attempts` /
`doh_attempts` / `rotate` / `use_local_ipv6` / `dns_over_https_config` /
`secure_dns_mode` / `allow_dns_over_https_upgrade` / `fallback_doh_nameservers`。
注入路径两条：`HostResolverManager::SetDnsConfigOverrides()`
（`net/dns/host_resolver_manager.h:201`，public）与
`HostResolver::ManagerOptions::dns_config_overrides`（正式构造路径，
`host_resolver_manager.cc:495` 消费）。`net/BUILD.gn:97` 定义了
`ENABLE_BUILT_IN_DNS`，说明默认 resolver 走 `DnsClient`，overrides 会真正生效——
否则改了也是空转。

**修法（ABI v9）**：

1. `mn_engine_config_t` 增加 DNS 字段（指针 + 长度成对，沿用现有结构体风格），
   `MN_ABI_VERSION` 8 → 9；三份导出表版本符号 `MINICRONET_2` → `MINICRONET_3`。
2. `crates/minicronet-sys/src/lib.rs` 同步 `#[repr(C)]` 与布局测试——注意 32 位
   分支要重算（`mn_engine_config_t` 加字段后 68/72 那类断言必须跟着改，
   且见下文「32 位 ABI 布局没有执行路径」，该分支目前无 CI 执行证明）。
3. Core 侧在 `Engine::InitializeOnNetworkThread`（`core/source/engine.cc`）
   构造 `DnsConfigOverrides` 并注入；参数校验沿用 `core/source/minicronet.cc`
   既有的 `kMax*` 上界风格，调用方传入的长度不可信。
4. 绑定层：`EngineConfig` 加字段（进缓存键）、`Session.__init__` 加参数、
   解除 `doh_url` 的拒绝分支；`.pyi` 由 `tools/expand-verb-signatures.py`
   重生成（该脚本幂等，`--check` 可断言），不要手改 24 处 verb 签名。

**必须先定的两个取舍**（不是实现细节，是语义决定）：

- **逐请求 vs 会话级**。DNS 是 engine 级设置（`set_*` 只在建 context 时生效）。
  若要做 `get(..., dns_options=...)`，每个不同配置都会重建 engine 并占用一个
  缓存槽——项目里已有同类教训（`_resolve_cookies` 注释记载「8 次调用方改动填满
  8 个槽并挤掉可复用那个」）。复用 `discard_cookies` 的 `_one_shot_slot` 可以规避
  缓存问题，但一次性 engine **会丢掉连接池与 TLS 会话缓存**，即逐请求 DNS 的请求
  享受不到 keep-alive。建议先只做**会话级**（进 `EngineConfig` 缓存键），语义清晰、
  无一次性 engine 代价。
- **`doh_url` 的语义映射**。curl_cffi 的 `doh_url` 是单个 URL，Chromium 是
  `DnsOverHttpsConfig`（多服务器 + 模板）。要么 1:1 映射，要么继续 fail-closed
  拒绝——按 AGENTS.md §8 不得静默忽略。

**验收**：

- 每个暴露的 overrides 字段都要有**可观测证据**证明它真的改变了 Chromium 行为
  （例如指向自定义 nameserver 后解析一个只有该服务器知道的域名）。按 AGENTS.md §7，
  没有实测的字段不能声明支持——`use_local_ipv6` 这类依赖系统栈行为的字段若无法
  证明，就 fail-closed 拒绝而不是暴露。
- 8 个平台产物与 manifest 全部重建并同步（`tools/install-core-binaries.py`）。
- 回归用例覆盖：默认路径（不传 DNS 参数）行为不变；非法 nameserver 报
  `InvalidArgument` 而非崩溃；DNS 配置不同的两个 Session 不串用解析结果。

## 待办：H3 全量强制测试会被端点限流

`tools/audit-profile-addition.py --levels 3 --protocol h3` 会为每个版本发 6 条真实 QUIC
请求到 `quic.tools.scrapfly.io`。对 93–157 全部 65 个版本连跑（390 次请求）时，端点会
开始拒绝部分连接：

```
response: RequestError { error: Protocol, net_error: -356 }
  即 ERR_QUIC_PROTOCOL_ERROR
live_connections: 5 / 1 / 4   （凑不满 6 条，无法比对）
```

**实测 2026-11**：全量 65 个版本跑下来 `passed 62, failed 3 [105, 106, 109]`，3 个失败
**全部**是这个 -356，且 `grep 'but the captures say'` 为 0 条——即**没有任何一条是字段
不符**。这 3 个版本单独重跑全部 PASS。同一版本连跑 6 次也全 PASS，说明触发条件是
**跨版本连续请求的总量**，不是单版本重复。

这不是指纹缺陷，但会让「全量 H3 验收」这条命令的结果不可信：一次全量扫描的 PASS/FAIL
里混着端点限流，而 FAIL 看起来和真缺陷一模一样。

**当前做法**：抽样跑。跨 era 边界取样（93/100/110/119/120/124/130/140/150/157），
覆盖 h3_text 的两个形态（93–119 无 SETTINGS_H3_DATAGRAM、120+ 有）与 ja4 的三个值。

**已做（2026-11）**：

- `--delay SECONDS`：每条连接之间等待，默认 0。
- `--retry N`：对 `-356` 重试 N 次（只重试这一个码；其他退出码不重试，因为那更可能是
  真的构建问题）。
- FAIL 分类：连不上 6 条时 `facts.verdict_kind = COULD_NOT_CONNECT`，文本输出显示为
  `3_binary: FAIL [COULD_NOT_CONNECT]`，与「字段不符」明确区分。连不上意味着这次运行
  **没有证明任何关于指纹的事**，不该和「这个字段错了」混在同一个失败列表里。

实测：之前失败的 `chrome_105 / 106 / 109` 在 `--delay 1 --retry 3` 下全部 PASS。

**仍待补**：全量 65 个版本在带延时的情况下跑一遍并记录结果（本次只验证了修复本身）。
判定条件：全量 65 个版本 0 个字段不符，且任何 COULD_NOT_CONNECT 都被如实标出而不是
当成 PASS。

## 待办：32 位 ABI 布局没有执行路径

`crates/minicronet-sys` 的布局测试已有 32 位分支（数值已按头文件修正为
84/28/92/32/80/16/8），但 CI 只跑 x86_64，所以 `linux-x86`、`windows-x86` 两个已
发布平台没有执行证明。需要在 CI 加一次
`cargo test -p minicronet-sys --target i686-unknown-linux-gnu`（或等价目标机运行）。

## 待办：net error 名称审计没有 CI 入口

`tools/audit-net-error-names.py` 逐行核对 `exceptions.py` 的 `_NET_ERRORS` 与 Chromium
的 `net/base/net_error_list.h`，但它需要一份 Chromium 检出，而两个 workflow 里都没有
带检出或缓存的 job。`ci.yml` 曾经写着「它由 Chromium-tree job 调用」，但那个 job 从来
不存在（`jobs:` 只有 `check` 和 `python-regression`，两者都不设 `CHROMIUM_SRC`）——
这正是 LESSONS 第 23 条「本地跑不到的门禁等于没有门禁」的同一形态：一条注释声称有远程
入口，实际只有开发机能跑。

现状：`NetErrorTableShapeTests` 在没有检出时校验表的**结构**（约 60 行、名字以 `ERR_`
开头、叶子类型正确、传输错误不得报成 TLS 或 chunked），这能在 CI 跑；但**名字与码的
对应关系**只能在有检出的机器上核对。

要么给 CI 加一个只拉 `net/base/net_error_list.h`（而非整棵 Chromium）的 job，要么把这份
头文件按 revision 缓存进仓库并加一条「缓存与 CHROMIUM_REVISION 一致」的门禁。

## 待办：产物来源证明

`core/binaries/*/manifest.json` 的 SHA-256 由写它的同一个工具生成，所以同时替换
二进制与 manifest 可以通过所有门禁。已加的检查只能保证 manifest 的
`chromium_revision` 与 `CHROMIUM_REVISION` 一致。要真正证明二进制来自这份源码，
需要一条能跑 Chromium 构建的 CI 路径：重建 → 比对 → 记录，或对产物签名/attestation。

## 待办：Linux 交叉构建的 pkgconf 未进仓库

`linux-x86` / `linux-arm64` 的交叉构建需要 `pkg-config`（gn 为 NSS 调用它）。
**获取方式已文档化**，见 [`BUILD.md`](BUILD.md) 的「Linux cross-builds need
pkg-config」一节：从发行版取三个 deb、解包到仓库外、用
`MINICRONET_PKGCONF_DIR` 指过去即可。仓库里不放这份工具（它是发行版包，
不属于本项目的产物）。

```bash
MINICRONET_PKGCONF_DIR=/tmp/pkgconf/root tools/build-core-linux-arm64.sh
```

两条**实测**结论，都与旧版本文档不同：

- `PKG_CONFIG_LIBDIR` **不需要设置**。旧文档要求显式指向
  `.../debian_bullseye_<arch>-sysroot/usr/lib/pkgconfig`，但
  `build/config/linux/pkg-config.py` 会自己从 `--sysroot` 推导并覆盖这个变量。
  用 `/nonexistent/bogus/path` 覆盖它仍然构建成功，且产物与不设置时逐字节相同
  （`069276f884924c4f`）。
- 不要设置 `PKG_CONFIG_SYSROOT_DIR`：gn 自己会传 `--sysroot`，再设一次会拼出双
  前缀（`.../sysroot/home/.../sysroot/usr/include/nss`），构建死在
  `pk11pub.h: file not found`。这一条已复现确认。

剩余项只是「要不要把 deb 也纳入仓库以支持离线构建」，不是「命令未知」。

完成条件是每项都有命令输出或目标机报告；“文件存在”不等于“平台支持完成”。
