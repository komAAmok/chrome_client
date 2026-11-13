# ABI v9 任务书与验收记录

**状态：已落地。** `MN_ABI_VERSION` 是 9，28 个导出符号，九个平台产物已重建。
下面的 §1 提案复核与 §2 批次划分保留为过程记录；§1.11 之后的四节是最终结果、
实现时修掉的缺陷、验证矩阵与未完成项。

本文件把 [ABI_V9_PROPOSAL.md](ABI_V9_PROPOSAL.md) 的十项改动拆成可独立验收的批次，
并记录在动手前对提案做的**事实复核**。提案仍是规格；本文件是执行顺序与验收记录。
状态列只承认被命令证明过的结论，未跑过的写「未验证」，不写「应该没问题」。

---

## 0. 动手前的基线（全部实测，2026-05-19 本机）

| 项 | 实测值 | 命令 |
| --- | --- | --- |
| 工作区 | 干净，HEAD `6cc6acd` | `git status --porcelain` |
| ABI | `#define MN_ABI_VERSION 8u`，20 个导出符号 | `tools/audit-abi.sh` |
| 布局 | `mn_engine_config_t` 152 / 21 字段，7 个结构体 70 字段一致 | `python3 tools/audit-abi-layout.py` |
| Chromium 检出 | `58bcc148f76b8975bbe59f1a6012d4ae409d62a9` = `CHROMIUM_REVISION` | `git -C $CHROMIUM_SRC rev-parse HEAD` |
| 九个 out 目录 | 全部存在且有 `build.ninja`（1.1–1.3 GB 各自） | `ls $CHROMIUM_SRC/out` |
| 热重建代价 | 只改 `core/source/minicronet/*` 时 48.6 秒（20 步，含重编 `engine.o` 与 SOLINK） | `ninja -C out/MiniCronet-linux-x86_64 libminicronet.so` |
| 交叉工具链 | `toolchains/{osxcross,macos-sdk}`、`xwin-winsysroot` 均在 | `ls $CHROMIUM_TOOLCHAIN` |
| Rust | `cargo/rustc 1.99.1`，在 `$HOME/.cargo/bin`（`~/.profile` 里那行 `. "$HOME/.cargo/env"` 指向不存在的文件，所以交互 shell 里 `cargo` 不在 PATH） | `$HOME/.cargo/bin/cargo --version` |
| 32 位布局 | `cc -m32 -S` 可用（freestanding），但**完整 `cc -m32` 编译缺 multilib 头**（`bits/libc-header-start.h` 不存在）。因此 32 位结构体尺寸只能用不依赖 libc 头的探针核对，不能跑 `-m32` 的完整编译 | `cc -m32 -c /tmp/probe32.c` |

### 一条会改变计划形状的约束

`mn_engine_config_t` 的 `size` 校验是**精确相等**（`core/abi/minicronet.h:55-58`
的契约 + `core/source/minicronet.cc:214-219` 的实现）。所以：

- **不能**先发一个「只有 8 个新符号、结构体不动」的 v9，之后再补字段——那会出现两个
  都自称 v9 而 `sizeof` 不同的库，调用方无法区分。
- 因此 v9 的全部`mn_engine_config_t` 字段必须与版本号在同一次提交里落地，
  并且**每一个字段都必须真的生效或被 fail-closed 拒绝**（AGENTS §8），
  不允许「字段先占位、行为以后补」。

这一条决定了批次划分：符号类（1/2/2.5/6）与字段类（3/4/5/7.1/7.2/7.4/8）
必须一起进 v9，只能靠「先写完全部 Core 侧代码，再一次性重建九个平台」来控制风险，
不能靠「分两次 bump」。

---

## 1. 提案复核：九处必须更正的断言（全部带行号证据）

| 编号 | 提案原文 | 实测 | 后果 |
| --- | --- | --- | --- |
| 1.1 | 「Core 零命中 hsts，会先发明文 `http://`」 | builder 无条件装 `TransportSecurityState`，预置列表已编进产物 | HSTS **已生效**，7.1 不是补能力 |
| 1.2 | 「桌面端 `EchMode` 恒 `kDisabled`」 | 空 getter 时默认 `kOpportunistic`，历史 profile 另有分支 | `NOT_ATTEMPTED` 的原因要改写 |
| 1.3 | ECH 三值状态 | `ssl_info` 只有一个 bool | `MN_ECH_FAILED` **不可达**，必须声明 |
| 1.4 | 结构体「约 56 字节」 | 实测 152 → 248 | 布局断言必须实测，不得手算 |
| 1.5 | 「25 → 27」与「20 → 28」并存 | 逐项表是 8 个 | 以 28 为准 |
| 1.6 | `hsts_enabled` 字段 | 关不掉（CHECK / 非虚 / 只收单标签） | **删掉这个字段** |
| 1.7 | 8.5 换默认 UA | 该字面量是产物版本门禁的标记 | 必须同提交换标记并做变异测试 |
| 1.8 | —— | 半实现的 ABI 面违反 §5 | 本轮回退，只留验证过的改动 |
| 1.9 | —— | 布局门禁的注释解析缺陷 | 已修 + 已变异测试 |
| 1.10 | —— | `proxy_chain` 顺序 / `dns_*` 注入点 | B3 必须按行为断言 |

### 1.1 「Core 完全没有启用 HSTS」不成立

提案 §7.1 的依据是 `grep -rniE 'hsts' core/source/engine.cc` 零命中。那只证明
**engine.cc 没有配置 HSTS**，不证明 HSTS 关闭。真实链路：

```
net/url_request/url_request_http_job.cc:354-357
  if (TransportSecurityState* hsts = request->context()->transport_security_state()) {
    upgrade_decision = hsts->GetSSLUpgradeDecision(url.GetHost(), ...);
  }
net/url_request/url_request_context_builder.cc:416-417
  context->set_transport_security_state(
      std::make_unique<TransportSecurityState>(hsts_policy_bypass_list_));   // 无条件
net/features.gni:48
  include_transport_security_state_preload_list = !is_cronet_build          // minicronet 构建为 true
```

结论：**v8 已经会按预置列表把 `http://` 升级为 `https://`**，因为 builder 无条件装了
`TransportSecurityState`，且预置列表编进了产物。提案 §7.1 的「本库现在会先发明文
`http://`」是错的。7.1 因此从「补一个缺失能力」降级为「补一个开关」——而开关本身
有实现障碍，见 1.6。

### 1.2 「桌面端 `EchMode` 恒为 `kDisabled`」不成立

```
net/url_request/url_request_context_builder.cc:392-397
  std::unique_ptr<net::EchModeGetter> ech_mode_getter;
  #if BUILDFLAG(IS_ANDROID)  ech_mode_getter = ...  #endif
  context->set_ssl_config_service(std::make_unique<SSLConfigServiceDefaults>(std::move(ech_mode_getter)));
net/ssl/ssl_config_service_defaults.cc:19-24
  if (ech_mode_getter_) return ech_mode_getter_->GetEchMode(hostname);
  return EchMode::kOpportunistic;                     // ← 空 getter 时是 Opportunistic
```

Core 走两条路：`chromium_current` 用 `SSLConfigServiceDefaults`（空 getter →
**kOpportunistic**）；历史 profile 用 `ProfileSSLConfigService::GetEchMode`
（`profile_ssl_config_service.cc` 末段：`profile_.ech_enabled ? kOpportunistic : kDisabled`）。
两者都不是「恒 kDisabled」。

ECH 仍然**不会发出**，但原因不是平台关掉了它，而是 `ssl_config.ech_config_list` 为空：
它只来自 DNS 的 HTTPS 记录（`net/socket/ssl_connect_job.cc:404-412`），而 Core 的解析
不取 HTTPS RR。所以 `mn_request_ech_status` 的 `NOT_ATTEMPTED` 应当被文档解释为
**「没有 ECHConfigList」**，而不是「平台关闭」。

### 1.3 `MN_ECH_FAILED` 在 ABI 里不可观测

Chromium 侧能读到的只有一个布尔：

```
net/socket/ssl_client_socket_impl.cc:523
  ssl_info->encrypted_client_hello = SSL_ech_accepted(ssl_.get());
net/http/http_response_info.h:190
  SSLInfo ssl_info;
```

「尝试了但被拒」与「没尝试」都表现为 `false`；区分二者的唯一代码是
`net/socket/ssl_client_socket.cc:105-118` 的 `ECHResult` UMA 直方图，不上任何 ABI 表面。
按 AGENTS §7「不可得的数据要如实声明不可得」：

- 保留三值枚举（协议上未来可扩展），但 `MODERN` 实现**只能返回
  `NOT_ATTEMPTED` 或 `SUCCESS`**；
- 头文件注释与本任务书都要写清 `FAILED` 当前不可达及其原因，
  不得让调用方以为 `FAILED` 是可达状态。

### 1.4 结构体增量的真实数量级

提案 §11.1 按「HSTS 2 + cookie 1 + 代理链 2 + platform 1」估了 56 字节，漏了 DNS 的
2 指针 + DoH 模板、host mapping 的 2 字段，也没算 4 字节标志组的对齐填充。

用真实编译器量（`cc` 读真实头文件，`cc -m32 -ffreestanding` 量 32 位）：

| 形状 | 64 位 | 32 位 |
| --- | --- | --- |
| 含 `hsts_enabled`（草案，已放弃，见 1.6） | 152 → 248 | 84 → 136 |
| 不含它（v9 实际形状） | 152 → 248 | 84 → 136 |

两行数字相同不是笔误：去掉一个 `uint8_t` 后，`dns_padding` 之后的 `cookie_samesite_override`
顶上来补位，两个 u8 组的填充总量不变，`platform`（enum，int 宽）仍在 240。
**这正是「不得手抄推算值」的理由**：手算会得出 240，而实测告诉你少一个字节不改变总数。

最终偏移（64 位，探针实测）：`host_mapping_rules 152 / dns_nameservers 168 /
doh_template 184 / hsts_bypass_list 200 / proxy_chain 216 / dns_secure_mode 232 /
cookie_samesite_override 236 / inject_client_hints 237 / platform 240 / sizeof 248`；
`mn_cookie_t` 88（32 位 52）。

### 1.5 符号总数：提案正文自相矛盾

§1.2 写「v8 的 20 个 → v9 的 28 个」，§6.3 写「v9 的 25 → 27」，§11.1 表格合计
「8 个新符号（20 → 28）」。以 §11.1 的逐项表为准：cookie 4 + 协商协议 1 + ECH 1 +
mTLS 2 = **8**，总数 **28**。§6.3 的「25 → 27」是残留文字。

### 1.6 关键障碍：`hsts_enabled = 0` 在当前 Chromium 上做不成

这一条是实现中撞出来的，它推翻提案 §7.1 的 ABI 形状。

提案设想的关闭方式是「不装 `TransportSecurityState`」。走一遍会发现三条路全断：

```
net/http/http_network_session.cc:153-157   TSS 传进 SSLClientContext
net/socket/ssl_client_socket.cc:229-230    CHECK(cert_verifier_);
                                           CHECK(transport_security_state_);
net/socket/ssl_client_socket_impl.cc:1278  context_->transport_security_state()->CheckPublicKeyPins(...)
net/socket/ssl_client_socket_impl.cc:1300  context_->transport_security_state()->ShouldSSLErrorsBeFatal(...)
```

`set_transport_security_state(nullptr)` 会让**每一次 TLS 握手** CHECK 崩溃（不是关 HSTS，
是 abort 宿主进程）。另外两个替代方案：

| 路径 | 为什么不通 |
| --- | --- |
| `LOAD_SHOULD_BYPASS_HSTS` | `url_request_http_job.cc:370-372` 对它 `CHECK(request->allow_credentials() == false)`，而该标志只有 `set_disallow_credentials()` 会清（`url_request.cc:594-596`）。要关 HSTS 就得连带关 cookie 与 auth |
| `hsts_policy_bypass_list` | `transport_security_state.cc:246-248` 对每条 `DCHECK(host.find('.') == npos)`，只接受单标签 TLD；写 `intranet.local` 在 release 下被静默忽略 |
| 子类只关静态查表 | `GetSSLUpgradeDecision` / `ShouldUpgradeToSSL` / `ShouldSSLErrorsBeFatal` 都不是 virtual（`transport_security_state.h:224-262` 逐个确认） |

**结论：v9 不提供 `hsts_enabled`。** `hsts_bypass_list` 保留，但 Core 必须先校验单标签并
拒绝多标签条目，否则就是「接受但静默失效」。

配套必须写进文档的事实：`kHstsTopLevelNavigationsOnly` 是 `FEATURE_DISABLED_BY_DEFAULT`
（`net/base/features.cc:690`），所以 HSTS 升级对 Core 的非导航请求**同样生效**。

### 1.7 §8.5「默认 UA 不再是库名」与门禁直接冲突

提案要把 `kDefaultUserAgent`（`minicronet.cc:48`）从 `minicronet/<version>` 换成 Chrome 串。
但那个字面量同时是**产物版本门禁的标记**：

```
tools/audit-core-binaries.sh:264-279
  artifact_version=$(strings -a "$path" | grep -oE 'minicronet/[0-9]+\.[0-9]+\.[0-9]+' ...)
  [[ -n "$artifact_version" ]] || die "$target: no minicronet/<version> marker in the artifact"
  [[ "$artifact_version" == "$workspace_version" ]] || die "... rebuild the Core after a version bump"
```

注释写明「The marker is the default User-Agent」，也就是说 8.5 落地后这道检查会**必然失败**。
所以 8.5 不是一个 `minicronet.cc` 的改动，它必须同时提供一个新标记（例如把版本串单独
编进一个 `constexpr` 供 `strings` 找到），并且这个新标记要经过一次变异测试：改掉它，
门禁必须非零退出。同一提交里完成，否则仓库停在红门禁上。

### 1.8 取舍：先回退，再一次性落地

第一次尝试时 Core 只写到一半就撞上 §1.6，于是把 ABI 面回退到 v8——「头文件声明了 v9
符号、Core 没有实现」正是这份 ABI 明确拒绝的状态。回退后实测 `tools/audit-abi.sh` 20
符号绿、`audit-abi-layout.py` 152 字节绿，仓库没有停在红门禁上。

随后按 §2 的批次把 Core 全部写完再一次性切换版本号：九个平台重建、manifest 刷新、
门禁整表跑过。**最终状态见 §1.11。** 保留这段是因为「先回退再重做」是当时唯一不违反
§5 的选择，而这个判断本身值得留档。

### 1.9 顺手修掉的一个门禁缺陷（已变异测试）

`c_header_structs()` 的结构体体部正则 `[^{}()]*` 刻意排除圆括号，避免把函数原型吃进
字段列表。副作用是：**任何注释里带圆括号，整个结构体就解析不出来**，报「declared in
Rust but not in the header」。v9 给字段加文档示例（`"8.8.8.8:53,1.1.1.1"`）时第一次
触发。修法是在解析前剥离注释，而不是把「注释不许有圆括号」写成规矩。

变异测试（`core/abi/minicronet.h` 里加一行带括号的注释）：

| 状态 | 结果 |
| --- | --- |
| 加注释 + 修复后 | `7 structs, 70 fields agree`，exit 0 |
| 加注释 + 修复前 | `mn_engine_config_t: declared in Rust but not in the header`，非零 |
| 恢复原状 | exit 0 |

### 1.10 Core 侧草拟时撞到的两处必然缺陷（未提交，已回退）

按 §1.8 回退的 Core 草稿里有两点值得留档，因为它们是提案设计层面的问题，
下一次动手会原样再撞一次：

**其一：`proxy_chain` 与 `set_proxy_config_service` 的顺序陷阱。** 代理链要生效，
必须改写 `ProxyConfig::ProxyRules`，而 builder 在 `Build()` 时才读那份配置。
草稿把改写写在 `set_proxy_config_service(...)` 之后、`Build()` 之前——语法上编译得过，
语义上是「接受了参数但可能不生效」，正是 AGENTS §8 禁止的那类缺陷。
正确顺序是**先**改 `proxy_config_`，**再**把它交给 builder。这条必须写进 B3 的验收：
用两个不同代理链建两个 engine，断言各自走各自的链，而不是只看 `MN_OK`。

**其二：`dns_*` 的注入点依赖 `HostResolver::CreateStandaloneResolver`，
而它的第一个参数需要 `NetLog`，此时 `context_` 还不存在。** 草稿里写的是
`context_->net_log().net_log()`，编译会直接失败。正确做法是先建 context 再设 resolver，
或者用 `NetLog::Get()`。这不是笔误级别的细节：它是「DNS 配置能不能在不 patch Chromium
的前提下接上」这个问题的答案，而答案取决于 `set_host_resolver` 与 `Build()` 的先后。

两条都说明同一个结论：**v9 的每个 config 字段都要先确认它在 builder 生命周期里的
正确插入点，再写 ABI 字段**，而不是反过来。B3 的验收用例必须按「行为」断言，
不能按「调用返回 MN_OK」断言。


### 1.11 落地结果（v9 已完成）

十项里能落地的都已落地，实测如下。「已完成」的判据是门禁与真实请求，不是编译通过。

```
MN_ABI_VERSION            9
导出符号                  28（v8 的 20 + cookie 4 + 协商协议 1 + ECH 1 + mTLS 2）
mn_engine_config_t        152 → 248（64 位）/ 84 → 136（32 位），38 字段
mn_cookie_t               88（64 位）/ 52（32 位）
mn_request_config_t       144（不变）
mn_request_callbacks_t    48（不变）
```

九个平台全部重建，`abi_version=9`，sha256 全部变化（判据：任一个没变就是没重建）：

| 平台 | 字节 |
| --- | --- |
| linux-x86 / x86_64 / arm64 / armv7 | 8,220,976 / 8,584,752 / 8,230,432 / 6,210,504 |
| windows-x86 / x86_64 / arm64 | 7,635,456 / 9,588,224 / 8,286,208 |
| macos-x86_64 / arm64 | 8,102,888 / 7,443,504 |

三条体积上限按各自的 2% 规则重新推导（linux 8,760,000 / windows 9,780,000 /
macos 8,270,000），注释里的推算过程与产物数字同步改。

### 1.12 实现时撞到的六个真实缺陷（都已修，都留了现场）

这一节的存在理由和 §1.10 相同：它们是**写的时候不做就会静默错**的地方。

| # | 缺陷 | 如何暴露 | 修法 |
| --- | --- | --- | --- |
| 1 | `mn_engine_cookies_get` 把 cookie 字符串放在栈上的 `CookieView` 里，函数一返回指针就悬垂 | 读回的 `name` 是空串而 `name_length` 是 3 | 存储挂到 `mn_engine` 句柄上，生命周期与 ABI 注释一致 |
| 2 | `std::vector<std::string>` 存字符串，扩容重分配使已发出的指针失效 | 同上，且只有第 4 个字段之后才坏 | 换成 `std::deque`（`push_back` 不搬移已有元素） |
| 3 | 复用同一 `CookieView` 却不先清空，每次调用累积 | 两次 `get` 后 count 从 1 变 2 | 每次进入先 `clear()` |
| 4 | `DeleteMatchingCookiesAsync` 末尾的 `FlushStore` 在无后端时 `PostTask`，而等待就发生在那条 runner 上 → 自锁 | delete 挂住不返回（`timeout` 60s 触发） | **两次修**：先把计数推迟兑现；后来发现谓词是同步执行的（`cookie_monster.cc:951-993`），改为在谓词里计数，`removed` 恢复为本次调用的真值 |
| 5 | `cookie.Domain()` 用前导点编码 host-only 标志，与调用方写的域名永不相等 | delete 返回 `removed=0` 而 cookie 还在 | 比较时接受两种拼写（`DomainWithoutDot()`） |
| 6 | 版本脚本节点 `MINICRONET_2` 被改成 `MINICRONET_3`，所有已编译绑定加载失败 | `audit-python36-artifact.py` 报 `version MINICRONET_2 not found` | 节点名保持 `MINICRONET_2`；ABI 版本由 `MN_ABI_VERSION` 表达 |

第 1 与第 2 条是同一类错误的两个层次：**ABI 里返回指针时，指针的存活期必须由某个明确
的对象持有**。把这条写进 §4 的风险表，比记住某一行代码更有用。

### 1.13 验证矩阵（实测，非推断）

| 验证 | 命令 / 形态 | 结果 |
| --- | --- | --- |
| ABI 一致性 | `tools/audit-abi.sh` | 28 符号，五处载体一致 |
| 布局（C ↔ Rust） | `python3 tools/audit-abi-layout.py` | 8 结构体 102 字段，248/88 |
| 布局（32 位） | `cc -m32 -ffreestanding` + Rust 断言 | 136/52 |
| 九个产物 | `tools/audit-core-binaries.sh` | 9 targets passed |
| 产物字节一致性 | `tools/audit-core-payloads.py` | 27 artifact/payload pairs |
| cookie 写读删清 | 真实 `libminicronet.so` + 真实 server | set/get/delete/clear 全通 |
| 协商协议 | 真实 h1 请求 | 响应前 `INVALID_STATE`，响应后 `version=3`（HTTP/1.1） |
| ECH 状态 | 同上 | `status=0`（NOT_ATTEMPTED），与 §1.2/1.3 一致 |
| fail-closed | 非法 source url / 跨域 domain | 都返回 `MN_ERROR_INVALID_ARGUMENT` |
| Python 绑定 | 七个测试模块 | 349 用例通过 |
| python36 产物 | `tools/audit-python36-artifact.py` | abi 9，能发一个真实请求 |
| profile 探针 | `tools/audit-profile-probes.sh` | 特性边界 + 隔离通过（state isolation 需 HTTP_BASE，标为 SKIPPED 并写明原因） |

### 1.13b 第二轮推进：两项关闭、三项如实定位

第二轮处理第一轮列为「未完成」的五项。逐项结论：

**① H2 session params 纳入 profile —— 已完成。**

抓包里确实有 `http2.sent_frames`，含 SETTINGS 的完整 id/值列表与 HEADERS 的帧标志。
72 个 profile × 6 条 = **432 条连接**逐条检查：SETTINGS 只出现
`HEADER_TABLE_SIZE` / `ENABLE_PUSH` / `MAX_CONCURRENT_STREAMS` /
`INITIAL_WINDOW_SIZE` / `MAX_HEADER_LIST_SIZE` 五个定义值，**没有一条带 GREASE id**；
也没有任何保留类型的帧。

对照源码，三个开关全部默认关闭：`network_session_configurator.cc:146-153`（grease
settings）、`:161-166`（greased frame）、`:173-178`（end-stream-with-data），每个都需要
命令行开关或 field trial 才置 true。所以 72 个 profile 三者皆 false。

`RuntimeProfileData` 因此加三个 `bool`，生成器写入，`engine.cc` 把它们写进
`HttpNetworkSessionParams`；`chromium_current` 分支保持 Chromium 自己的默认（那正是
current 的含义）。新增 `tools/audit-h2-framing.py` 双向校验：抓包不得出现 GREASE id
或保留帧，编译进产物的表也必须写 false。变异测试：表里一个 profile 改 true → 非零；
抓包塞一个保留帧 → 非零；恢复 → 零。

**② `http2_end_stream_with_data_frame` 的证据边界，必须写明。**

抓包里所有 HEADERS 都带 `EndStream`，但那是 **GET 无 body** 的形态。这个开关只在
**有 body 的请求**上才改变行为（`spdy_session.h:427` 的访问器只在有上传时被读）。
所以「抓包证明它为 false」在无 body 样本上**不可证**：开关开或关，GET 都在 HEADERS
上结束。当前取 false 的依据是**源码默认值**，不是抓包。这条区分不写清楚，下一个人
会以为抓包覆盖了它。

**③ Chrome 默认 CookieAccessDelegate —— 前提不成立，关闭而非实现。**

第一轮把它列为「需要实现 delegate 且是行为变更」。复核源码后这个前提是错的：
**没有 delegate 时的默认语义已经是 Chrome 默认。**

```
net/cookies/cookie_base.cc:554-575  GetEffectiveSameSite()
  case CookieSameSite::UNSPECIFIED:
    return (access_semantics == CookieAccessSemantics::LEGACY)
               ? NO_RESTRICTION
               : (recently_created ? LAX_MODE_ALLOW_UNSAFE : LAX_MODE);
```

无 delegate 时 `GetAccessSemanticsForCookie()` 返回 `UNKNOWN`
（`cookie_monster.cc:2668-2673`），而 `UNKNOWN != LEGACY`，所以未写 SameSite 的 cookie
**已经是 Lax**，正是 Chrome 的默认。`cookie_base.cc:236,284,372,383` 与
`:141,173,186,211` 的 scope 分支同样只在 `== LEGACY` 时改变行为。

也就是说：注入一个「实现 Chrome 默认语义的 delegate」，只要它不返回 LEGACY，行为与
现状完全相同；返回 LEGACY 才会把 cookie 变成 `NO_RESTRICTION`，那是倒退。所以这一项
的正确结论是**不改代码**，并把证据留下——而不是加一层看起来对齐了 Chrome 的包装。
`cookie_samesite_override` 保持只接受 0，理由现在更硬：0 已经是对的。

（`ShouldIgnoreSameSiteRestrictions` 是 delegate 的第三个纯虚方法，它在
`services/network` 里被用于实验室开关；这一层不在 ABI 的可达范围内，也不影响上面的
结论：默认路径的 SameSite 判定已与 Chrome 一致。）
**④ client hints 的 Core 侧序列化 —— 已完成。**

第一轮说「72 份 UserAgentMetadata 是独立证据工程」。复核后要连续更正两点：

一是数据本来就有：`sec-ch-ua` / `sec-ch-ua-platform` / `sec-ch-ua-mobile` 等 12 个头
的值**已在 72 个 profile 的 h1 抓包里**，facade 的 `CAPTURED_HEADERS` 早已从它们生成。

二是它确实值得搬进 Core：`SetExtraRequestHeaders` 正是提案 §3 指定的注入点，而
facade 拼的头要经过 `prepared.prepare()` 才到同一条路径——Core 自己做，`Accept-CH`
协商看到的就是真实浏览器的请求，而不是绑定层事后补过的。

实现：生成器把捕获的头按「名字集合」去重后编进产物（**3 组**而非 72 组：9 / 11 / 12
个头，39 KB 文本），`RuntimeProfileData` 增加 `browser_header_set` 索引；
`request.cc` 从 `GetBrowserHeaderSet()` 取出、重写两个依赖宿主的字段、再
`MergeFrom(caller)` 合并。

**两个必须重写的字段**，否则就是「报告了抓包那台机器」：

| 字段 | 来源 |
| --- | --- |
| `User-Agent` 的括号组 | `mn_platform_t`（与 `platform_hint()` 同源） |
| `sec-ch-ua-platform` | 同上 |

版本号仍来自 profile 自己的抓包。`Host` 与 `Connection` 在生成器里被丢弃：前者由
URL 决定，后者是 ABI 明确拒绝的 hop-by-hop 头。

实测（真实 `libminicronet.so` + 回显服务器）：

```
profile=chrome_155, inject_client_hints=1
  发出 12 个捕获头 + caller 的 X-Custom
  sec-ch-ua-platform: "Linux"        ← 跟随宿主，不是抓包的 "Windows"
  User-Agent: ... (X11; Linux x86_64) ... Chrome/155.0.0.0 ...
  Accept-Language: en-US,en;q=0.9     ← caller 覆盖生效

profile=chrome_155, inject_client_hints=0
  只发 caller 的头（4 个），无任何捕获头

chromium_current（无 profile）
  只发 caller 的头，默认路径未被污染（提案 §3.2 的防回归要求）
```

`inject_client_hints` 因此从「置 1 被拒绝」变为**真正生效的开关**：1（默认）注入，
0 抑制，>1 拒绝。
**⑤ B4 绑定层删补偿 —— 未做，范围已定位。**

Core 已能直接读写 store，所以 `_resolve_cookies` 的 read-compare-replace 与
`EngineConfig.generation` 已是纯累赘。删它们要同时改 `sessions.py` 三处
（`_resolve_cookies`、`_slot_for` 的缓存键、`_one_shot_slot`），并让 349 个既有用例
继续通过。现有测试全部依赖当前行为，改它必须连测试一起改，属于独立一件事。

### 1.13c B4 绑定层删补偿 —— 已完成

这一项第一轮评为「未做，需连测试一起改」。做完了，过程里有四个真实缺陷被测试抓出来。

**删掉的东西**（都是为 v8 的不可读 store 而存在）：

| 载体 | 为什么可以删 |
| --- | --- |
| `EngineSlot.mirror` + `cookie_header` / `absorb` | store 可读，镜像没有可对照的对象 |
| `EngineConfig.generation` 及其缓存键维度 | store 可就地清空，不需要「另一个空引擎」 |
| `_one_shot_slot` / `_discard_serial` | discard 用 `mn_engine_cookies_clear` 实现 |
| `_resolve_cookies` 的 read-compare-replace | jar 写回 store，两者不会再分歧 |
| `_open_websocket` 的分歧换引擎分支 | 同上 |

**新增的东西**：`EngineSlot.cookies/set_cookie/delete_cookie/clear_cookies`（Core 直通）、
`write_cookie/remove_cookie/clear_cookies`（jar 写回的三个入口）、`Session._engine_cookies*`
四个门面方法、`RequestsCookieJar(sink=...)` 写回机制。

**测试抓出的四个缺陷**，都不是编译期能发现的：

| # | 缺陷 | 暴露方式 | 修法 |
| --- | --- | --- | --- |
| 1 | jar 写回 store 时没有加锁，6 个线程里 3 个同时进入 store 写入 | 并发测试的峰值断言 `3 != 1` | sink 的三个入口取 `_cookie_lock` |
| 2 | discard 请求之后 store 里残留 `sid=BBB` | 隔离测试断言失败 | 请求**之后**再清一次：`Set-Cookie` 是 Chromium 自己收下的，不是 jar 写的 |
| 3 | `Session(cookies={...})` 的 cookie 从不进 store | echo 回空 | 首个请求时 flush 一次（那时才有 URL 可作为 source） |
| 4 | per-request `cookies=` 覆盖不了 store 里的同名 cookie | `extra=2` 没出现 | 请求级 cookie 先写进 store，再发请求 |

还有一个语义要点：**每个 engine 一个 store**，所以 `verify=False` 这类 override 会落到
一个空 store 上。`_seed_store_from_jar` 在第一次使用某个 slot 时把 jar 灌进去，每个
slot 只灌一次——否则每次请求都会抹掉那个 engine 自己收到的 cookie。

**实测**（真实 server）：

```
set-cookie 之后:  jar=[sid=server]  store=[sid=server]  echo=sid=server
调用方改 jar 之后: jar=[sid=caller]  store=[sid=caller]  echo=sid=caller   ← 关键
```

第二行是这一整项的价值：调用方的编辑现在直接到达线上，不再经过一次「发现分歧 → 换引擎」。

### 1.13d 用桌面的完整抓包集交叉验证（`完整/` 三个文件）

桌面 `完整/` 里是 19.4 MiB 的原始抓包：`tls-results.json`（h1+h2）、`tls-results-h1.json`、
`tls-results-h3.json`，按 Chrome 大版本分组，每版本带 `status` 元数据。**结论：它是仓库
`profiles/` 的同一批原始数据的副本，不是新证据** —— 逐版本比对（86/100/124/155/157 的
SETTINGS 与 akamai 指纹）零差异，h3 覆盖也完全吻合（65 有抓包 + 7 个 unsupported）。
所以它**不改变任何一个 v9 结论**，也不该被提交进仓库（AGENTS §10：19 MiB 不是发布产物）。

但它有一个仓库抓包没有的东西：**逐样本 `checks`**，因此可以回答「这六条连接是不是真的
独立」——这是第 2 级要求里最难自证的一条：

| 事实 | 数据 |
| --- | --- |
| h2 样本总数 / 已恢复会话数 | 432 / **0** |
| h3 样本总数 / 已恢复会话数 | 432 / **0** |
| 每版本 `sessionId` 互异 | 72/72 版本都是 6/6 |
| 稳定字段六条全等 | 72/72 版本 |
| `client_random` 六条互异 | 72/72 版本 |

仓库抓包里同样有 `client_random` / `session_id`（已抽查 6 条互异），所以这一点仓库本身
已经覆盖；这批数据的价值在于**它是独立的一次采集**，而不是又多一份同样的断言。

**它独立复现的 v9 相关边界**（与编译表逐条一致）：

| 字段 | 边界 | 依据 |
| --- | --- | --- |
| `ech_enabled` | **119** | `extensionEncryptedClientHello` 首次出现 |
| `use_new_alps_codepoint` | **135** | `application_settings (17613)` 取代 `_old (17513)` |
| `advertise_h3_datagram` | **120** | H3 SETTINGS id 51 首次出现 |
| `enable_http2_settings_grease` | 全 false | 432 条连接无一条带数字字面量 SETTINGS id |
| `greased_http2_frame` | 全 false | 帧序列唯一：SETTINGS, WINDOW_UPDATE, HEADERS |

顺带一个结论修正：我最初写的 ALPS 扫描用 `samples[0]` 单样本匹配，得出「data 里没有
ALPS」，那是**扫描写错了**，不是数据缺失。改成扫全部 6 个样本后，边界 135 立刻显现。
这条记在这里，因为它正是 §1.13b 里「抓包证明为 false」那类判断容易出错的地方。

**因此新增门禁** `tools/audit-v9-profile-fields.py --evidence-dir DIR`：

它解决的问题是所有既有 profile 门禁的共同盲点——**表与抓包都是本仓库导入的**，
导入器漏掉一个字段时，两者会一致地错。这道门禁从第二份独立采集里重新推导边界，
再与编译表逐 profile 比对。四个变异全部捕获（ECH 在 100 上翻真、ECH 在 155 上翻假、
ALPS 在 120 上翻真、datagram 在 100 上翻真），恢复后转绿。

没有 `--evidence-dir` 时它输出 `unavailable` 并 **exit 0**：第二份数据在仓库外，
一个在输入缺失时静默通过的门禁比一个如实说不知道的更糟（AGENTS §13 的 `unavailable`
规则）。

### 1.13e h1/h2/h3 处理是否还需要优化 —— 逐项核查

结论：**协议层不需要优化**（它全部由 Chromium 负责，Core 不做协议分支，符合 §1）；
需要补的是**绑定层没接上的 v9 查询面**，已在这一轮接通 `http_version`。

核查过程与判据：

| 检查项 | 结果 | 判据 |
| --- | --- | --- |
| h2 参数是否漏配 | 不用改 | `avoid_h2_reprioritization` / `h2_connection_keepalive` / `enable_priority_update` / `use_dns_https_svcb_alpn` 四个未设置项，编译期默认全 `false`，且 Chrome 自己的 `network_session_configurator.cc` 同样不设置 |
| `enable_priority_update` 能否由抓包判定 | **不能** | 抓包只记录发出的帧（432 条全是 SETTINGS/WINDOW_UPDATE/HEADERS），而该值取决于**服务器**是否发 `SETTINGS_NO_RFC7540_PRIORITIES`，客户端抓包原理上看不到 |
| h3 fail-closed 是否真的生效 | 生效 | 实测：chrome_86/92 + FORCE_H3 → 15（PROFILE_UNSUPPORTED），chrome_93/157 → 0 |
| h1 有无协议分支 | 无 | `request.cc` 只在强制 v2/v3 时拒绝非 https，不做 h1 特化；h1 门禁只管 TLS 层 ALPS 缺失 |
| `http_version` 是否接通 | **原来没接通，已修** | see below |

#### 修掉的缺口：`http_version` 四层只通到第二层

Core 从 ABI v9 起就能报协商协议（`request.cc` 的 `OnResponseStarted` 读
`HttpResponseInfo::connection_info`），Rust 安全层也有对应能力，但**pyo3 与 facade
都没接**：facade 仍在调用 `_negotiated_http_version(status_line)`，从状态行推断。
表现是本地 h1 请求报 `http_version=None`。

接通方式是四处改动：`Response.http_version`（Rust）、`HttpVersion` 枚举与
`Request::negotiated_protocol()`、`PyResponse.http_version` + `PyRequest.http_version()`、
facade 的 `_finalize(http_version=...)`。

**为什么不把协议塞进事件元组**：`poll_events` 返回 5 元组，多处按位置解包；加第六个
元素会同时改掉所有解包点，而只有一个值需要它。改成 `PyRequest` 上的 getter，
异步泵在收到 response 事件时读一次即可。

**实测对比**：

```
修前:  pin=None -> http_version=None      pin=v1 -> http_version=None
修后:  pin=None -> http_version='HTTP/1.1'  pin=v1 -> http_version='HTTP/1.1'
```

两个测试因为断言了 v8 的旧行为而失败（`test_http_version_is_not_guessed`、
`test_response_attributes` 都断言 `None`），按 §7 改为断言正确行为，并把"不可知必须
报 None"这半条移到仍有回退路径的说明里。

**还剩一件事**：`mn_request_ech_status` 仍没有 getter，接法与协议相同。已记入
`NEXT_STEPS.md`。

### 1.13f B4 的五项：复核与两个新缺陷

五项上一轮都改过，这一轮复核查边界，测出两个新缺陷并修掉。新增测试文件
`bindings/python/tests/test_cookie_store_v9.py`（9 个用例），每个都写了动机的实测。

| # | 新缺陷 | 暴露方式 | 修法 |
| --- | --- | --- | --- |
| 6 | discard 请求**失败**时 `writing_suppressed` 永不复位，之后每次 cookie 写入都被静默丢弃 | 测试：连到拒绝端口后 `writing_suppressed` 仍为 True | 恢复放进 `try/finally`，覆盖所有异常路径 |
| 7 | `_seeded_slots` 以 `id(slot)` 为键，id 复用会把空 store 的新 slot 误判成已灌入 | 直接驱动 cache 的探针：6 次 get/discard 复用同一 id | 键改为 slot 对象本身 |

**一个我自己的判断错误，值得记下来。** 改成对象键之后我推断「被驱逐的 slot 失去引用，
CPython 会自动移除条目，所以剪枝是多余的」，并把剪枝删掉了。理由是只跑普通 `get()`
循环时集合确实停在 1 项。

这个推断**只在一半场景成立**：走 override 的请求（`verify=False`）被 Core 通过请求
持有引用，`gc.collect()` 也回收不掉。实测 8 次 `verify=False`、`max_engines=1`（17 次
build）后，集合里留着 **9 项**而只有 1 个活 slot。剪枝已恢复，`EngineCache.slots()` 也
因此必要。

**变异测试**（三个机制各破坏一次，确认测试会红）：

| 变异 | 结果 |
| --- | --- |
| `finally` 恢复删掉 | 失败 ✓ |
| 剪枝改成 no-op | 失败 ✓（集合涨到 9） |
| 键改回 `id()` | 失败 ✓（补了一条直接检验的用例才成立） |

**一个教训**：`id()` 那条变异一开始是**通过**的——我最初的用例在会话层面观察，而会话自己
持有旧 slot，id 复用根本不会发生。补了一条直接驱动 `EngineCache` 的用例（6 次 get/discard
实测确实复用 id）之后变异才失败。断言写不到机制上，就是没测到。

### 1.13g 第三轮：写回路径的四条失败分支

顺着 §1.13f 那两个缺陷的**同一个模式**（标志/状态在失败路径上没复原）继续查写回，测出三个
新缺陷。测试文件 `test_cookie_store_v9.py` 从 9 个用例增到 12 个。

| # | 缺陷 | 暴露方式 | 修法 |
| --- | --- | --- | --- |
| 8 | `_pending_store_cookies` 在取到 `source` **之前**就清零，一旦没有 URL，整批预置 cookie 永久丢弃 | 直接驱动该守卫 | 清零移到整批写完之后 |
| 9 | `session.cookies.set()` 在 store 拒绝时抛异常，但 jar 里**已留下**该条目——调用方看到失败，`session.cookies` 却显示成功 | `set(..., domain="not a domain")` 实测：抛错且条目仍在 | `set_cookie` 捕获后回滚 jar 再重抛，做到全有或全无 |
| 10 | jar 里一条无法写入的 cookie 会让 `_seed_store_from_jar` 整体抛错，**所有** cookie 都送不到新引擎 | 预置一条坏 domain 后走 override 路径 | 逐条隔离，单条失败继续 |

第 10 个的第 4 条同类路径（`_resolve_cookies` 里的 per-request cookie 循环）也补了隔离。

**值得记的一点**：第 8 个的守卫在正常 API 下不可达——`default_source` 在 `prepare()`
之后、
flush 之前就已就绪。我先写了一条走公开 API 的测试，它**通过**了，于是看出测试根本没碰到
那个分支。改成直接驱动守卫，才真正锁住行为。变异测试确认：清零提前 → 测试红。

**变异测试**（三项新机制，破坏即红）：

| 变异 | 结果 |
| --- | --- |
| flush 清零提前到取 source 之前 | 失败 ✓ |
| `set_cookie` 失败后不回滚 jar | 失败 ✓ |
| seed 逐条隔离改成整体 | 失败 ✓ |
| per-request 逐条隔离改成整体 | 失败 ✓ |

### 1.13h 第四轮：从一个不变量代替逐个发现

前三轮修了 10 个缺陷，都是同一个形状：**一个标志或一份 jar 停在 store 从未到达的状态**。
所以这一轮不再逐个找，而是把不变量本身写成测试。

```
任何失败之后：jar 可写（writing_suppressed 为 False），
且 jar 里的每条 cookie 都是 store 已经接受过的。
```

`test_cookie_store_v9.py` 新增 8 个用例覆盖八种失败模式：拒绝连接、拒绝连接 + discard、
服务端 500、500 + discard、`raise_for_status`、`max_response_bytes` 超限、jar 赋值被拒、
超时。每个用例失败后都断言同一个不变量，并真的发一次请求验证 cookie 能到线上。

**变异确认**这两条断言确实锁住了前几轮的修复：

| 变异 | 结果 |
| --- | --- |
| 去掉 `try/finally` 里的 mute 恢复 | 失败 ✓ |
| 去掉 jar 回滚 | 失败 ✓ |

#### 查过但不成立的怀疑（记录下来，避免下轮重复）

| 怀疑 | 实测 | 结论 |
| --- | --- | --- |
| failed discard 请求泄漏 fd（40 次 +6） | 20/100/180 次三点采样 fd 恒为 10，增量 0/0 | 缓存预热，不是泄漏；`tools/leak-probe.py` 五种形态全 flat |
| `_content_consumed` 的 11 处赋值会在读取失败时误标 | 逐处读代码，都在成功之后 | 不是缺陷 |

`tools/leak-probe.py` 五形态结果：`sync-get` / `sync-stream` / `sync-abandon` / `async-gather` /
`error-path` 全部 PASS（`async-gather` fd +6 在 ≤8 容差内，是既有行为，未改动）。

### 1.13i 按 AGENTS §1.1 重做分层审计：12 处越界，全部有据可查

新增约束 §1.1 后，按它把绑定层重扫了一遍。结论：**12 处逻辑属于下层，仍留在绑定层**。
每一处都记进了 `tools/audit-layer-ownership.py` 的 `KNOWN`，连同阻塞迁移的**具体**
下层缺口与复核行号。

| 越界 | 位置 | 阻塞原因（可复核） |
| --- | --- | --- |
| 重新解析原始头块 | `models.py` `parse_raw_headers` | ABI 只送 `raw_headers()`，没有结构化头数组（`minicronet.h:396`） |
| 从状态行推断协议 | `models.py` / `.pyi` / `sessions.py` 调用点 | 仅用于重定向中间跳与缓存命中，此时无 Core 响应可问 |
| 从状态行取 reason | 同上 | `HttpResponseInfo::headers` 里的 reason 没进回调 |
| hop-by-hop 头表 | `sessions.py` `_HOP_BY_HOP_HEADERS` | Core 接受任意头名，拒绝只能发生在上层；需 `request.cc:245` 改为返回 `MN_ERROR_INVALID_ARGUMENT` |
| Core 拥有头表 | `sessions.py` `_CORE_OWNED_HEADERS` | `SetExtraRequestHeaders` 只增不减，删除是 no-op 且 Core 无法报告（`request.cc:275`） |
| WS 握手保留头表 | `sessions.py` `_FORBIDDEN_WS_HEADERS` | 握手头块在 Chromium 内部构造，ABI 看不到哪些名字是保留的 |

**没有列入的**（判定为归属正确，留个记录避免下次重复讨论）：

- `_split_timeout`：v9 的 `mn_request_config_t` 仍只有一个 `timeout_ms`，
  所以 `timeout=(connect, read)` 拒绝是正确的 fail-closed，不是越界。
- `_PROXY_SCHEMES` / `_NATIVE_RULE_KEYS` / `_PROXY_SCHEME_ALIASES`：代理 URI 的**拼写**
  归一化，属于对外 API 的参数接受面；`ParseFromString` 的语法由 Core 负责。
- `RequestsCookieJar` 的 dict 接口、`stream=True` 的可迭代形状：requests 的 API 契约。

**门禁的三条判定规则**（都不依赖「看起来像」）：

1. 新增一处未登记的越界 → 失败。
2. `KNOWN` 里有条目已不再匹配任何代码 → 失败（迁移完成或模式腐烂，都不许留在表里）。
3. 测试文件不参与扫描：测试可以点名它正在验证的规则，被审计的是发布的绑定。

**变异测试**（三种，全部捕获）：

| 变异 | 结果 |
| --- | --- |
| 在 `models.py` 新增 `_STATUS_TEXT = {...}` | 失败 ✓（第一版漏掉了状态码表模式，补 pattern 后才捕获） |
| 在 `KNOWN` 插入一条指向不存在文件的条目 | 失败 ✓ |
| 恢复 | 通过 ✓ |

第一条变异一开始**没被捕获**——这暴露了 `PATTERNS` 覆盖不足，而不是门禁正确。补上
`_STATUS_*` / `_REASON_*` 两个模式后才生效。记在这里，因为「门禁通过」和「门禁能抓」
是两件事。

### 1.14 状态总表

| 项 | 状态 | 依据 |
| --- | --- | --- |
| 第 2 项 协商协议 / 2.5 ECH 状态 | **完成** | 真实 h1 请求：响应前 `INVALID_STATE`，响应后 `version=3` |
| 第 6 项 mTLS | **完成** | 两个 setter 落在 `SSLClientContext`（`ssl_client_socket.h:216`） |
| 第 1 项 Cookie 存取 | **完成** | 写读删清全通，四类 fail-closed 均拒绝 |
| 第 5 项 Host mapping / 第 4 项 DNS | **完成（Core）** | 仅 linux-x86_64 能真实加载，其余八平台靠构建 + payload 校验 |
| 第 7.1 项 HSTS | **改为 bypass 列表** | 关闭开关不可达，见 §1.6 |
| 第 7.2 项 Cookie 语义 | **前提不成立，不改代码** | 默认已是 Lax，见 1.13b ③ |
| 第 7.3 项 H2 session params | **完成** | 三个 bool 进 profile 表，`audit-h2-framing.py` 双向守 |
| 第 8 项 platform 身份 | **完成（Core）** | UA 组与 `sec-ch-ua-platform` 同源；facade 侧未接 |
| 第 3 项 client hints | **完成（Core）** | 捕获头编进产物，注入与抑制两个方向都实测 |
| 第 9 项 非幂等重发 | **已在前一版完成** | `no-resend-non-idempotent.patch` |
| 第 10 项 hop-by-hop 头 | **已在前一版完成** | 六字段整组拒绝 |
| B4 绑定层删补偿 | **完成** | 镜像 / generation / one-shot 全部删除，见 1.13c |

已知失败（不在本轮范围内）：`test_compat.test_process_pool` 在**未改动的 HEAD 上同样失败**
——`multiprocessing` 的资源追踪器在 194 个测试同进程运行时 `BrokenPipeError`。用 `git stash`
在干净树上复现了同一失败，而该模块单独跑、整个 `ConcurrencyTests` 单独跑都通过。
按 AGENTS §12 记为已知失败，不 skip、不隐藏。

`mn_engine_cookies_delete` 的 `removed` 是**同步真值**：谓词同步执行
（`cookie_monster.cc:951-993`），在里面计数即可，不必等那个会自锁的完成回调。

---

## 2. 批次划分（依赖顺序，每批一次可验收的落地）

「批次」是**提交边界**，不是「ABI 版本边界」：所有批次完成前不 bump
`MN_ABI_VERSION`，最终由 B5 一次性切换版本号并重建九个平台。

### B0 · 门禁与基线（不改行为；本轮的 1.9 已属于这一步）

- 目标：让 v9 的每一类改动都有会失败的检查先行存在（AGENTS §6 变异测试）。
- 文件：`tools/audit-abi.sh`、`tools/audit-abi-layout.py`、
  `tools/abi-exports.py`、新增 `tools/audit-platform-identity.py`（提案 §8.6）、
  新增 `tools/audit-ech-status.py`（守 1.3 的不可达性）。
- 验收：`tools/audit-abi.sh` 在**故意**删掉一个符号声明时非零退出；恢复后零退出。
  `audit-platform-identity.py` 在把 `kDefaultUserAgent` 改回 `minicronet/` 时非零退出。
- 回滚点：单个提交，只动 `tools/`。

### B1 · 头文件与 FFI 契约（布局一次性定型）

- 文件：`core/abi/minicronet.h`（`MN_ABI_VERSION 9u`、`mn_cookie_t`、
  `mn_cookie_same_site_t`、`mn_http_version_t`、`mn_ech_status_t`、`mn_platform_t`、
  8 个原型、`mn_engine_config_t` 的 13 个新字段）、
  `crates/minicronet-sys/src/lib.rs`（`#[repr(C)]` 结构 + 8 个 `pub fn` + 两套布局断言）。
- 验收：`python3 tools/audit-abi-layout.py` 输出 248（32 位 136）且零退出；
  `cc -m32` 探针与 Rust 断言逐字段相等。
- 回滚点：头文件与 FFI 一起回退即可，此时 Core 尚未实现新符号，`audit-abi.sh` 会红——
  **这是 B1 的预期中间态，必须在同一个提交里带上 Core 的空实现桩（返回
  `MN_ERROR_INVALID_STATE`）才能提交**，否则仓库停在红门禁上。

### B2 · Core 的四个符号组（cookie / 协商协议 / ECH / mTLS）

- 文件：`core/source/minicronet.cc`（8 个 `MN_CALL`）、`core/source/engine.{h,cc}`
  （`cookie_mutex_`、`WaitableEvent` 桥、`SSLClientContext` 取用）、
  `core/source/request.{h,cc}`（`negotiated_protocol` / `ech_status` 的完成后可读状态）。
- 关键实现约束（都已核实行号）：
  - cookie 回调在 `CookieMonster` 自己那串上执行（`net/cookies/cookie_store.h:42-53`），
    必须 `PostTask` 回 `Engine::task_runner()` 再 `Signal`，不能在回调里摸 `mn_` 句柄。
  - `connection_info` 的取值必须走 `response_info().connection_info`
    （`net/http/http_response_info.h:161`）+ `HttpConnectionInfoToCoarse`
    （`net/http/http_connection_info.cc:127`）；粗化会丢掉 0.9/1.0，
    所以 `0.9/1.0/1.1` 三个值读原始枚举，`HTTP2/QUIC/OTHER` 读粗化结果。
  - mTLS 走 `context()->http_transaction_factory()->GetSession()->ssl_client_context()`
    → `SetClientCertificate`（`net/socket/ssl_client_socket.h:216`），
    证书 `X509Certificate::CreateFromDERCertChain`（`net/cert/x509_certificate.h:103`），
    私钥 `WrapOpenSSLPrivateKey`（`net/ssl/openssl_private_key.h:21`）。
    PEM 解析失败必须 `MN_ERROR_INVALID_ARGUMENT`（fail-closed，含带口令私钥）。
- 验收：`docs/ABI_V9_PROPOSAL.md` §12 用例 2/3 与 §6.5 用例 1/3 在 v8 上失败、v9 上通过。
- 回滚点：`core/source/*` 单独可回退；此批不重建产物则九个 manifest 仍指 v8。

### B3 · Core 的 config 字段（host mapping / HSTS / DNS / 代理链 / platform / 两个开关）

- 逐字段的落点与 fail-closed 判据：

| 字段 | 落点（已核实） | 不满足时的行为 |
| --- | --- | --- |
| `host_mapping_rules` | `builder.set_host_mapping_rules`（`url_request_context_builder.h:276`） | 语法交给 `HostMappingRules`，Core 不自写解析器；空串 = v8 行为 |
| ~~`hsts_enabled`~~ | **删除**：见 §1.6，没有可用的关闭点 | — |
| `hsts_bypass_list` | `builder.set_hsts_policy_bypass_list`（`:324`） | 多标签条目在 release 下会被 `DCHECK` 之外静默忽略 → **必须在 Core 侧先校验单标签并拒绝**，否则是静默失效 |
| `dns_*` / `doh_template` | `HostResolver::ManagerOptions::dns_config_overrides`（`net/dns/host_resolver.h:361-362`）+ `HostResolver::CreateStandaloneResolver`（`:608`）+ `builder.set_host_resolver`（`:271`） | nameserver/DoH 模板解析失败必须返回 `MN_ERROR_INVALID_ARGUMENT`；`doh_template` 非空而 `dns_secure_mode==0` 视为冲突 |
| `proxy_chain` | `ProxyChain(std::vector<ProxyServer>)`（`net/base/proxy_chain.h:46`）+ `ProxyList::SetSingleProxyChain`（`net/proxy_resolution/proxy_list.h:45`） | 与 `proxy_rules` 同时非空 → `MN_ERROR_INVALID_ARGUMENT`（两份代理配置只能有一个） |
| `platform` | UA 由 `Core` 决定（`minicronet.cc:143-153`），facade 的 hint 必须同源 | 与 `profile` 的 UA 冲突检测（`minicronet.cc:357-363`）必须放行「平台不同、版本相同」 |
| `inject_client_hints` | v9 只留开关，数据仍由 facade 提供（提案 §3.1） | 置 1 而 Core 无数据 → `MN_ERROR_INVALID_ARGUMENT`，不静默忽略 |
| `cookie_samesite_override` | 0 = Chrome 默认；提案 §7.2 明确不实现策略开关 | 1-3 → `MN_ERROR_INVALID_ARGUMENT` |

- 验收：提案 §5.6 三条、§7.5 第 1/2/3 条、§8.7 四条。
- 回滚点：本批与 B2 都是 `core/source`，但字段语义互不依赖，可分别回退。

### B4 · 绑定层（删补偿、加真值）

- 删除（提案 §1.4 逐条）：`_resolve_cookies` 的 read-compare-replace、
  `EngineSlot.cookie_header` 镜像与 `_cookie_lock` 的 generation 推进、
  `EngineConfig.generation` 及其缓存键维度、`_open_websocket` 的分歧换引擎分支、
  `discard_cookies` 的 `_one_shot_slot`。
- 新增：`Response.http_version` 真值、`Response.ech_status`、
  `Session(client_cert=...)`、`Session(host_mapping=...)`、`hsts_bypass=`、`platform_override=`。
- **不提供** `hsts=`：§1.6 之后没有可关闭的开关。文档必须写明「HSTS 恒为 Chrome 的默认
  行为，只能按单标签 TLD 绕过」，而不是留一个看起来能关的参数。
- 未实现项的 fail-closed 必须保留并在文档里指出：`interface=`（提案只说「解除拒绝」，
  但 v9 的 config 里没有 interface 字段，因此**必须继续拒绝**）、`doh_url` 与
  `client_cert` 的带口令私钥。
- 验收：`python3 -m unittest discover -s bindings/python/tests -p "test_*.py"` 全绿；
  新增用例覆盖 §12 用例 1/3 与 §6.5 用例 2。

### B5 · 产物、门禁与文档收口

- 九个平台重建 → `tools/install-core-binaries.py --abi-version 9` → `targets.json` 改 9。
- 三份导出表：`.lds` 的 `MINICRONET_2` → `MINICRONET_3` 并加 8 个符号。
- 默认 UA 换掉 `minicronet/<version>` 时，`tools/audit-core-binaries.sh:264-279` 的标记必须
  同提交换成新来源，并做一次变异测试（见 §1.7）。这一步没做就等于把版本门禁关掉。
- 文档：`docs/ABI.md` 新章、`docs/COMPATIBILITY_BOUNDARY.md`（`http_version` 与
  cookie 双 store 两节作废；`cert`/`hsts`/`host_mapping` 三条改写）、
  `docs/NEXT_STEPS.md`、两份 README 能力表、`AGENTS.md:36` 的「ABI v8，20 个符号」。
- 验收：AGENTS §5 的整张门禁表；九个 manifest 的 `sha256` 全部变化（任一个没变
  就说明该平台没重建）。
- 回滚点：一次提交，产物与 manifest 同进同退。

### 不在 v9 范围内（提案自己排除）

- 第 3 项 client hints 的**数据**（72 份 `UserAgentMetadata` 证据）；
- 第 7.3 项把 `enable_http2_settings_grease` / `greased_http2_frame` /
  `http2_end_stream_with_data_frame` 纳入 `RuntimeProfileData`（要重跑 72 个 profile 的
  第 2、3 级证据）。
两者都只留入口，不承诺行为。

---

## 3. 提交时序（一次 bump，避免中途红门禁）

```
B0  tools/          ← 新增检查先红（变异测试）后绿
B1  header + sys    ← 与 Core 桩函数同提交，audit-abi.sh 保持绿
B2  core symbols    ← 只测 linux-x86_64 一个平台，manifest 暂不刷新
B3  core fields     ← 同上
B4  bindings        ← 依赖 B2/B3 的真实行为
B5  版本号 + 9 平台重建 + manifest + 文档   ← 唯一一次 bump，之后整表门禁
```

**B1 与 B5 之间 `audit-core-binaries.sh` 会红**（manifest 说 8、头文件说 9）。
这是设计上的必然：那个检查同时校验头文件版本与九份 manifest，任何 ABI bump 都必然
经过这段窗口。处置方式是**在 B5 的同一个提交里**完成重建，不把窗口留在 main 上。

---

## 4. 风险登记（按「会不会产生新 bug」排序）

| 风险 | 为什么危险 | 处置 |
| --- | --- | --- |
| cookie 同步桥的锁序 | `WaitableEvent` 等待 `CookieMonster` 回调，而回调要 `PostTask` 回 network runner；若请求线程持有 `cookie_mutex_` 又等 network 线程，网络线程正等这把锁 → 死锁，表现是宿主进程挂住 | 锁只在**发起**时取、在 `Wait()` 之前释放；加一个两线程交替的 `--gtest_repeat` 压测再合 |
| 把 `kUNKNOWN` 报成 `HTTP/1.1` | 提案 §2.3 的核心收益就是不再谎报；图省事就白做 | `audit-ech-status.py` 与 §12 用例 2 第二段专守 |
| `MN_ECH_FAILED` 可达性 | 见 1.3，Chromium 只给一个 bool | 头文件注释 + 门禁断言「实现里没有任何路径返回 FAILED」 |
| 有人照 §7.1 去实现 HSTS 关闭 | 三条路都断；置空 `TransportSecurityState` 会让每次 TLS 握手 CHECK 失败（§1.6） | 字段已删除；`hsts_bypass_list` 的多标签条目在 Core 侧拒绝，不交给 Chromium 静默忽略 |
| 结构体增长影响所有调用方 | 152 → 248（32 位 84 → 136）是精确相等校验，任何按旧头文件编译的调用方都会 `MN_ERROR_INVALID_ARGUMENT` | 这是 v9 的预期契约变更，进 release note；不提供兼容层 |
| 九平台重建不全 | 有一个平台的 `sha256` 没变就是没重建，而单平台漏建在功能上完全看不出来 | B5 用 `audit-core-binaries.sh` 的 sha/size/架构/导出表四重检查兜底 |
