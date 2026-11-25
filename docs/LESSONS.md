# 经验教训（LESSONS）

本文件是项目的**踩坑记忆**：每一条都来自一次真实的错误、错误结论或差点放过去的缺陷，
记录「现象 → 根因 → 规则 → 现在由什么门禁守住」。

写新代码或改门禁前先读这里。新增一条时按同样的四段式写，并**同时**补上门禁——
没有门禁的教训会在下一次重构时重演。

---

## 1. 版本号只能有一个来源

**现象**：`chrome_client.core_version()` 返回 `0.4.0`，而 `__version__` 是 `0.2.5`。

**根因**：`core/source/minicronet.cc` 里手写 `constexpr char kVersion[] = "0.4.0"`。
commit `1947054`（「版本号不再需要手动同步」）删掉了四处副本并加了反向门禁，但
`tools/audit-pypi-metadata.sh` 只扫描 README / RUST_API_FREEZE / `__init__.py` /
pyproject，**漏掉了 Core 自己这一份**。

**规则**：发布版本只存在于根 `Cargo.toml` 的 `workspace.package.version`。任何需要
版本号的地方都必须从它派生，禁止第二份字面量。

**门禁**：`tools/version.py` 是唯一读取点；`tools/sync-core.sh` 用它生成
`minicronet_version_generated.h`（提交的是 `0.0.0-generated` 占位符，构建时覆盖）。
`mn_version_string()` 返回该值，所以 Core 版本与 Python 版本不可能再分叉。

---

## 2. 中间层会抹掉协议真相

**现象**：对 `tls.peet.ws` 请求，服务端回报 `http_version: h2`、Akamai 指纹
`52d84b11737d980aef856699f885ca86`，客户端 `response.http_version` 却是 `HTTP/1.1`。

**根因**：HTTP/2 响应的状态行在 Chromium 里被规范化为 `HTTP/1.1 200`
（`net/spdy/spdy_http_utils.cc`），Core 透传 `raw_headers()`，facade 于是从**状态行文本**
推断协议版本。ABI v8 根本不携带协商结果。更糟的是 `test_compat.py` 把这个 bug
写成了断言（`assertEqual(response.http_version, "HTTP/1.1")`），把缺陷固化下来。

**规则**：不能从派生数据反推事实。要么让 ABI 暴露真实字段，要么如实声明「不可得」——
本项目对 `reason` 就是这么做的（声明从标准表推导），两种态度必须一致。

**门禁**：`http_version` 现在返回 `None` 并附带说明；回归测试用真实 H2 服务器断言
「不得谎报 HTTP/1.1」。新增 ABI 字段后应改为返回真实值。

---

## 3. 流的两种模式必须都能取到 body

**现象**：`AsyncSession.get(url, stream=True, allow_redirects=False)` 返回
`len(content) == 0`，同步路径同样参数却正常。

**根因**：`_AsyncState` 在流式模式下把数据推进 `chunks`、把 `body` 留成 `None`；
`sessions.py` 的非原生重定向分支却读 `state.body`。而「非原生重定向」的触发条件
不只是 `allow_redirects=False`，还包括 `max_redirects < 30`——隐蔽的第二个入口。

**规则**：一个状态对象有两条缓冲路径时，任何出口都必须先问「另一条路径里有没有数据」。
新增读取点时不要假设模式。

**门禁**：回归测试覆盖 `allow_redirects=False`（唯一能进入 manual 模式的入口；
`max_redirects` 的数值形式已在 0.2.9 改为拒绝，见 `docs/COMPATIBILITY_BOUNDARY.md`），
同步/异步各测一次。

---

## 4. 文档承诺必须与代码一致（fail-closed 是自己的招牌）

**现象**：`discard_cookies=True` 的文档写着 "Neither send nor record cookies"，
实测请求仍然带着 `sid=ABC`。

**根因**：facade 只删了 `Cookie` 头，但 Core 的 Chromium `CookieMonster` 才是权威，
它会重新附加自己存的 cookie。`discard` 只影响了 Python 侧。

**规则**：本项目对外的核心承诺是「无法忠实实现就显式报错，而不是静默忽略」。
凡是被这个承诺覆盖的选项，必须在**两个存储/两层实现**上都生效，否则报错。

**门禁**：`discard_cookies` 现在把该请求放到一个**一次性 Engine** 上
（`sessions.py`，`_throwaway_slot`），它的 store 只有这一次请求能看见。

> 更正（2026-11）：这一条此前写的是"切换到一个空 cookie store 的同配置 Engine"，
> 而代码在 ABI v9 之后改成了`slot.clear_cookies()` **原地清空会话共享的 store** +
> 会话级 `writing_suppressed`。单线程下两者等价，并发下不等价：实测一个普通请求与一个
> `discard_cookies` 请求并行时，20 次读里 19 次拿到错误的 `Cookie` 头（别人的值或空）。
> 见 §43 后面的补记与本轮审计报告 P0-2。回归测试只覆盖了单线程，所以当时全绿。

---

## 5. 不能证明的断言就是错的断言

**现象**：`crates/minicronet-sys` 的 ABI 布局测试在 32 位分支断言
`engine_config=88 / websocket_callbacks=28 / websocket_config=76`。

**根因**：拿真实头文件为 32 位目标编译（`gcc -m32`，`size_t = unsigned int`）得到的是
**84 / 32 / 80**。测试表写错了三个结构体，而 CI 只跑 x86_64，所以
`linux-x86`、`windows-x86` 这两个**已发布平台**从来没有过 ABI 证明。

**规则**：测试里的魔数必须来自被测量对象本身，不能来自记忆或推导。跨平台的分支要有
跨平台的执行路径，否则就明说「本分支未被验证」。

**门禁**：数值已按头文件修正并注明来源。**遗留**：CI 仍只跑 64 位，
需要交叉目标上执行 `cargo test --target i686-unknown-linux-gnu`。

---

## 6. 配置写了不等于生效

**现象**：根 `Cargo.toml` 有 `[workspace.lints.rust] unsafe_op_in_unsafe_fn = "deny"`。

**根因**：cargo 只对**显式声明** `[lints] workspace = true` 的成员应用 workspace lints。
三个成员 crate 都没写，所以这条 deny 一直是**惰性**的——看起来有安全网，实际没有。

**规则**：任何「中心化配置」都要验证它真的作用到了目标上，方法是在目标里故意违反一次、
看门禁是否失败。

**门禁**：四个 manifest 都已加 `[lints] workspace = true`；验证方式是一次故意违规
（`unsafe fn` 里裸解引用）确认报 `E0133`，随后回滚。

---

## 7. 门禁必须能被证伪

**现象**：审计脚本经常看起来在检查，实际永远通过。

**规则**：新增或修改 `tools/audit-*.sh` 后，必须做一次**变异测试**：
故意破坏被检查的东西（删一个导出符号、改一个 manifest 哈希、让一个 stub 参数名对不上），
确认脚本**以非零退出**，然后恢复。没做过变异测试的检查不能算检查。

**门禁**：`tools/audit-abi.sh` 的 `.def` 提取范围、`audit-core-binaries.sh` 的架构与
导入库哈希、`audit-readme.sh` 的相对链接正则、`audit-python-stubs.py` 的参数表比对，
均已用变异测试确认会失败。

---

## 8. 提交的二进制没有来源证明

**现象**：8 个平台的 `core/binaries/*` 已提交，`audit-core-binaries.sh` 校验
「文件与它旁边的 manifest 一致」。

**根因**：manifest 由**写它的同一个工具**生成（`install-core-binaries.py` 复制文件后
计算 sha256）。所以同时替换 `.so` 与 manifest 可以通过全部门禁。Linux 产物还被
`--remove-section=.note.gnu.build-id` 与 `.comment` 剥掉了可追溯信息。

**规则**：哈希自洽不是来源证明。要证明「这个二进制来自这份源码」必须重新构建并比对，
或者由 CI 产出并签名。

**门禁**：已加「manifest 的 `chromium_revision` 必须等于 `CHROMIUM_REVISION`」。
**遗留**：仍缺「重建并比对」的 CI 步骤，需要能跑 Chromium 构建的 runner。

---

## 9. 门禁的盲区就是下一处漂移

**现象**：同一批文档里同时存在 `0.2.3`（实为 0.2.5）、`19 exported C functions`
（实为 20）、`Chrome 99--151`（实为 99–153）、指向已删除 `new/` 目录的路径。

**根因**：每一条都曾被某个门禁**部分**覆盖，但都恰好落在扫描范围之外
（例如版本号门禁扫 README 却不扫 PROJECT_STATUS）。

**规则**：文档里的可验证事实要么被门禁覆盖，要么就不要写成确定语气。
修改任何门禁时，先问「还有哪些同类载体没被扫到」。

**门禁**：版本号门禁现已覆盖 `docs/PROJECT_STATUS.md`；ABI 符号数与 profile 范围
由 `audit-abi.sh` / `generate-profile-table.py` 的可复现性检查覆盖。

---

## 10. 大文件进 git 历史就再也删不掉

**现象**：`.git` 目录 677 MB，可达 blob 合计 **1257 MiB**。

**根因**：`core/binaries` 的 97 个历史版本（833 MiB）、
`bindings/python36/target` 的 776 个构建产物 blob（235 MiB）、
早期 vendored 的 `cronet-*`（101 MiB）都进过历史。删掉文件不会删掉历史。

**规则**：发布产物可以提交（本项目需要它让 CI 能离线跑），但**每次重建前先想清楚
旧版本是否必须留在历史里**。构建输出（`target/`）永远不进版本库。

**门禁**：`.gitignore` 已不再忽略 `core/binaries` 下的产物（之前忽略导致重建的 Core
不出现在 `git status`、`git add` 静默无效）；`bindings/*/target/` 保持忽略。
**遗留**：历史瘦身需要一次 `git filter-repo` 并强制推送，属于破坏性操作，须单独决定。

---

## 11. 平台相关的常量不能硬编码

**现象**：所有 profile 的 User-Agent 都是 `(X11; Linux x86_64)`，包括在 Windows 上跑的
Core；而 `profiles/chrome_153/h2/captures.json` 记录的抓包是
`(Windows NT 10.0; Win64; x64)`。

**根因**：`HistoricalUserAgent()` 里写死了平台令牌，而 profile schema 里**没有**
platform 字段，所以它不是数据驱动的。

**规则**：profile 拥有的是**版本号**（版本级的 wire 差异）；**平台**是运行环境的事实，
不能由 profile 冒充——否则同一份 profile 在 Linux 上会自称 Windows。
运行环境相关的分支要用 `BUILDFLAG(IS_WIN/IS_MAC)` + `ARCH_CPU_*`。

**门禁**：已按平台分支（Windows / macOS / Linux x86_64 / aarch64 / i686）。
**遗留**：若将来要支持「伪装成 Windows 客户端」，需要给 profile 增加显式 platform 字段
并让用户选择，而不是靠硬编码。

---

## 12. 子代理/审查结论必须复核

**现象**：一次深度审查给出 `[CRITICAL]` 结论「`OnReceivedRedirect` 会造成 read-buffer
UAF 与永久挂死」。

**根因**：审查者漏看了紧邻的两行——
`scoped_refptr<...> buffer = std::move(read_buffer_);`（所有权已移交回调）
与 `if (!read_buffer_) { ... }`（下次读会分配新 buffer）。结论因此不成立。

**规则**：任何外部结论（包括子代理、包括本文件）在写入代码或文档前都要回到源码验证。
**否定性结论同样需要证据**：说「不是缺陷」也要指出是哪两行使其不成立。

**门禁**：无自动化门禁（这是流程约束）。凡是引用他人结论的提交信息或文档，
必须附上自己复现/复核的命令或代码位置。

---

## 13. 别把交叉编译当成运行时验收

**现象**：README 对 Windows/macOS 的 Python 绑定打了 ✓，但 CI 里**没有任何 job
真正 import 过这些 wheel**；wheel 工作流只做 maturin 构建与文件名检查。

**规则**：构建成功 ≠ 能加载 ≠ 能发请求。平台矩阵的每一格都需要一次真实运行，
否则不要在 README 打 ✓。

**门禁**：`ci.yml` 已加「import 扩展并发出一次真实请求」的步骤。
**遗留**：Windows/macOS 的 wheel 仍只验证文件名，需要加对应平台的导入测试。

---

## 14. 构造参数被存储 ≠ 被使用

**现象**：`Session(proxy="http://...")` 完全不走代理——每个请求都直连，而 `response`
正常返回 200，没有任何报错。

**根因**：`_effective_proxy()` 只读 per-request 的 `proxy=`、`proxies=` 与 `self.proxies`，
从不读 `self.proxy`。构造函数把值存进 `self.proxy` 之后，那条路径就是死代码。

**规则**：新增一个配置入口时，必须验证它真的影响了最终行为。存储字段不等于生效，
尤其当同一个概念还有第二个入口（这里是 `proxies` 映射）时——第二个入口会被误认为
已经覆盖了第一个。

**门禁**：`test_session_proxy_constructor_is_used` 用一个**只在代理后面存在**的主机名
断言响应体，所以「直连成功」不可能伪装成通过。

## 15. 从上游复制常量表必须校验来源

**现象**：代理失败时报 `Proxy (net error -111)`，用户无法判断是隧道失败、认证失败
还是 SOCKS 问题。

**根因**：`exceptions.py` 的 `_NET_ERRORS` 表手工维护且已漂移——**8 个码挂着别的错误的
名字**（-336 是 `ERR_NO_SUPPORTED_PROXIES` 却写成 `ERR_TUNNEL_CONNECTION_FAILED`；
-348 是 `ERR_PAC_NOT_IN_DHCP` 却写成 `ERR_PROXY_AUTH_REQUESTED`），而代理失败真正产生的
`-111` / `-115` 根本不在表里；另有 `-215` 是上游已删除的码。

**规则**：常量表的值必须能从权威来源重新推导，且推导过程要能进 CI。凭记忆或凭
「看起来对」抄写，错的是名字，而名字正是用户唯一能据以行动的信息。

**门禁**：`tools/audit-net-error-names.py` 用 `net_error_list.h` 逐行核对（需 Chromium
检出），并强制要求代理相关码存在；`NetErrorTableShapeTests` 在没有检出时校验表结构。
已用变异测试确认改名会失败。

## 16. 同一个错误可能属于两个不同的所有者

**现象**：两个测试对「流式请求的错误何时抛出」有相反要求——一个要求 `get()` 抛超时，
另一个要求 `max_response_bytes` 在迭代时才抛。我先用「流式就延迟抛」一刀切，结果
打破了超时契约。

**根因**：两类错误的归属不同。**截止时间属于请求**（流式调用者等的是 header，不能让
它永远睡着）；**体积上限属于消费者**（同步路径就在迭代处报，异步必须一致）。

**规则**：在把一类错误统一处理之前，先问「这个错误是谁的」。用 `isinstance` 区分归属，
而不是用「是不是流式」这种代理指标。

**门禁**：`StreamingErrorOwnershipTests` 同时锁定两条契约，避免以后有人为了修一个
而打破另一个。

## 17. 上游的隐式行为要写进文档

**现象**：给 `http://127.0.0.1` 配代理时，请求**不经过代理**，直接连本机，且没有任何
提示。

**根因**：Chromium 对所有代理配置都隐式绕过 localhost 与 link-local
（`net/proxy_resolution/proxy_host_matching_rules.cc` 无条件追加
`SubtractImplicitBypassesRule`），而 ABI v8 只传 `proxy_rules`、没有 `bypass_rules`
字段，所以连 `<-loopback>` 这个逃生舱口也传不进去。

**规则**：凡是「行为与直觉不符且由上游决定」的点，必须写进兼容边界文档，并说明有没有
绕过办法。用户会把它当成 bug 反复报告。

**门禁**：无自动化门禁（需要 ABI 增加 bypass 字段）；已在
`docs/COMPATIBILITY_BOUNDARY.md` 与 `NEXT_STEPS.md` 记录。

## 18. 不稳定的门禁比没有门禁更糟

**现象**：`test_internationalized_host_is_canonicalized` 在全量运行时偶发失败，单独运行
却总是通过。

**根因**：它断言两种写法的**异常类完全相同**，而同一个无法解析的主机名会随解析时序报
`DNSError` 或 `Timeout`。断言过度指定了实现细节。

**规则**：测试应当断言**意图**而不是实现细节。这条测试的意图是「两种写法都被当作
同一个请求发出去，而不是被当成非法参数拒绝」，与具体异常类无关。

**门禁**：改为断言「不是 `ValueError`/`InvalidURL`，且不是 `ERR_INVALID_URL`」，即它真的
走到了网络层。

## 19. 上游限流不是本库的缺陷——先做对照再下结论

**现象**：16 个并发请求打同一个公网回显服务时大量 `ERR_TIMED_OUT`，看起来像并发缺陷。

**根因**：该服务对同一来源的并发连接限流。对照组是决定性的：同一台机器用 `curl` 发
16 个并发请求 3.9 秒全部 200；换成**本地** HTTPS 服务器后，32 并发（同 URL 与不同 URL）
全部成功，耗时 0.35 秒。库本身的并发机制没有问题。

**规则**：判断「是不是本库的问题」必须有一个不受被测对象影响的对照组（这里是 `curl`）
和一个**本地**复现环境。把公网服务的限流当成库缺陷，会去修不存在的问题。

**门禁**：并发相关的验收一律用本地服务器；公网只用于指纹保真度验证。

## 20. 错误分类要按协议分层，不能按「差不多」

**现象**：HTTP/2 流错误与 QUIC 失败报成 `ChunkedEncodingError`，HTTP/2 协议错误报成
`SSLError`。

**根因**：Core 把**所有** HTTP/2 与 QUIC 协议错误折叠成一个 `Protocol` 类别
（`core/source/minicronet/error_mapping.h`），而 facade 把这个类别映射到
`ChunkedEncodingError`——那是 HTTP/1.1 分块编码的名字。`SSLError` 更误导：握手已经成功，
调用者却去查证书。

**规则**：异常类型是调用者唯一的行动依据。传输层失败（h2/QUIC）与 TLS 失败、HTTP/1.1
分帧失败是三种不同的处置方式，不能共用一个名字。

**门禁**：`Protocol` 类别映射到 `ConnectionError`，`-337`/`-358` 改为 `ConnectionError`，
`ChunkedEncodingError` 只保留给 `ERR_INVALID_CHUNKED_ENCODING`；由
`test_transport_errors_are_not_reported_as_tls_or_chunked` 锁定。

## 21. 代理相关的隐式行为清单（速查）

排障时按这个顺序看，能覆盖绝大多数「代理配了但没生效 / 响应为空」：

1. **目标是不是 loopback**？是则 Chromium 隐式绕过代理（见第 17 条），直连且无提示。
2. **用的是哪个入口**？`Session(proxy=...)` 曾完全不生效（见第 14 条）；per-request
   `proxy=`、`session.proxies`、环境变量三条路径是独立的。
3. **报错名字**：`ERR_TUNNEL_CONNECTION_FAILED`(-111) = 代理拒绝或连不上源站；
   `ERR_PROXY_CONNECTION_FAILED`(-130) = 代理本身不可达；`ERR_PROXY_AUTH_UNSUPPORTED`(-115) /
   `ERR_PROXY_AUTH_REQUESTED`(-127) = 代理要认证；`ERR_EMPTY_RESPONSE`(-324) = 连上了但对端
   没回任何字节；`ERR_NO_SUPPORTED_PROXIES`(-336) = 规则解析后没有可用代理；
   `ERR_TOO_MANY_RETRIES`(-375) = 凭据被拒后 Chromium 在同一个 URLRequest 里重试了 32 次
   仍失败，所以「密码错了」不会以 407 的形式回到调用者手里（见第 22 条）。
4. **`NO_PROXY` 的语义**：facade 用它决定「用哪个 Engine」，命中就选直连 Engine，而不是
   让 Chromium 去绕过。
5. **带 realm 的挑战认证不了**？那是 ICU 数据集缺 `.cnv` 的旧缺陷（见第 22 条）。

## 22. 裁掉的数据要按消费者核对，不能按「看起来没人用」

**现象**：`Session(proxy=..., proxy_auth=("user", "pass"))` 对**带 realm** 的 407 挑战
永远认证不了——凭据从未发出，响应就是一个裸的 407，没有异常、没有日志、没有提示。
把 `Proxy-Authenticate: Basic realm="p"` 换成 `Basic`（不带 realm）就正常。

**根因**：Core 的 ICU 数据集为了省体积排除了 `conversion_mappings`，而 HTTP Basic/Digest
的 realm 解码恰好要用它：

```
net/http/http_auth_handler_basic.cc:53   ConvertToUtf8AndNormalize(value, kCharsetLatin1, realm)
base/i18n/icu_string_conversions.cc:202  CodepageToUTF16(text, "ISO-8859-1", FAIL, &utf16)
base/i18n/icu_string_conversions.cc:167  ucnv_open("ISO-8859-1", &status)   // 失败即 return false
```

`ISO-8859-1` 是 `windows-1252-html` 转换器的别名
（`source/data/mappings/convrtrs.txt:302`）。`ucnv_open` 失败 → `ParseRealm` 返回 false →
`CreateAuthHandler` 返回 `ERR_INVALID_RESPONSE` → `HttpAuth::ChooseBestChallenge` 静默丢掉
这个 challenge → `handler_` 为空 → `PopulateAuthChallenge()` 不被调用 →
`GetAuthChallengeInfo()` 返回 nullptr → `NotifyAuthRequired()` 从不发生 → Core 的
`Request::OnAuthRequired` 一次都不执行。**整条认证路径被一个「省 3 KB」的决定切断了，
而每一层都按「没有挑战」处理，所以全程无声。**

**规则**：裁数据前必须按**消费者**核对，而不是按「这个功能看起来用不到」。当时的断言是
「Core 从不调用 ICU 转换器」，它看起来有证据（Core 把响应头原样交给绑定层），但漏掉了
**Chromium 自己的 net 层**也在用同一个转换器。这类判断要 grep 上游调用点，而不是只看本仓库。

**门禁**：`filter.json` 改为 `includelist: ["windows-1252-html"]`（数据集 191,056 →
194,064 字节），并由 `test_compat.ProxyAuthenticationTests` 锁定——它让代理**真的**回 407
并断言响应体，所以「直连成功」和「凭据没发出去」都不可能伪装成通过。

**顺带两条**：
- 拒绝凭据时 Chromium 会在同一个 URLRequest 内重试到 `kMaxRestarts`(32)，最终报
  `ERR_TOO_MANY_RETRIES`(-375) 而不是 407。这个码原先不在错误表里，调用者只看到裸的
  `net error -375`；已补进 `_NET_ERRORS` 并纳入 `audit-net-error-names.py`。
- ICU 的 `CommentStripper` 只剥离**行首**的 `//`，带缩进的注释会让 `configure` 直接
  `JSONDecodeError`。改 `filter.json` 时注释必须顶格。

**探针纪律**：这次是靠往 `Request::OnAuthRequired` 里塞 `fprintf` 才定位的，而第一版探针
在 `OnResponseStarted` 里调了 `response_headers()->GetStatusLine()`，把
`network-error` 这个 smoke 场景**稳定打崩**（6/6 SIGSEGV，而发布版 0/6）。探针也是代码，
它跑在真实线程上：加探针后必须先做「发布版 vs 探针版」对照，再拿它的输出下结论。

## 23. 本地跑不到的门禁等于没有门禁

**现象**：0.2.6 的 `Rust structure CI` 在 `main` 与 `v0.2.6` 上各失败一次，报 8 个
`name-defined` 错误：`_types.pyi` 用了 `Literal` 却没导入，`sessions.pyi` 用了
`Tuple` 却没导入。**v0.2.5 的同一项检查是干净的**（`Success: no issues found`），
所以这是当轮引入的回归，不是历史遗留。

**根因**：mypy 只写在 `.github/workflows/ci.yml` 里，**本地门禁清单里没有它**。
`AGENTS.md` 第 5 条当时列了 11 条命令，没有一条做类型检查；我逐条跑完全绿，
于是把一个只有 CI 能发现的缺陷推了上去。

**规则**：一条门禁如果本地跑不到，它就只能在**远程**发现问题——而远程是最慢、最贵、
且已经公开的位置。凡 CI 里有的检查，本地清单里必须有等价的一条；两边最好调同一个
脚本，而不是各写一份命令。

**门禁**：新增 `tools/audit-python-typing.py`，把 CI 的两条 mypy 命令收进一个脚本，
`ci.yml` 与 `AGENTS.md` 第 5 条都指向它。缺 mypy 时打印安装提示并跳过（类型检查是
可选依赖，不该让没装它的检出失败）。变异测试：删掉 `Tuple` 导入 → 非零退出；删掉
`Literal` 导入 → 非零退出；恢复后通过。

**另一层**：这两个错误都是「存根引用了没导入的名字」。mypy 对未定义名字只报
`name-defined` 并继续，不会中止，所以如果 CI 步骤是「报告但不失败」，它们会一直
躺在日志里。门禁的退出码必须被真正检查，不能只看日志有没有内容。

---

## 24. 只存在于开发机的构建输入，在干净克隆上必然失败

**现象**：4 个 profile 校验探针（`profile_isolation_probe.c`、
`profile_feature_probe.cc`、`profile_state_isolation_probe.cc`、
`websocket_extended_connect_probe.c`）在仓库里根本不存在——`git ls-files | grep -i probe`
只返回 `core/source/profile_probe.c`，`core/patches/` 里也没有任何补丁创建它们。它们
只躺在开发机的 `$CHROMIUM_SRC/minicronet/` 里。

**根因**：`tools/sync-core.sh` 先按 glob 安装 `core/source/*.{c,cc}`，然后用一个
**硬编码的四个名字**校验「它们存在」，注释还写着「fail loudly rather than silently
dropping targets BUILD.gn still declares」。这个校验在开发机上是**永远为真**的：文件本来
就躺在目标目录里，glob 不覆盖它、校验也照样通过。于是「同步源码」这台机器上全绿，任何
干净克隆都会在 `die "$DEST/$probe missing"` 处失败。`core/source/BUILD.gn` 一直声明着
这四个 target，仓库却拿不出对应源码——构建输入的所有权从未被检查过。

**规则**：构建输入的所有权是**可验证事实**，不是记忆。「这个文件在我机器上存在」与
「这个仓库拥有这个文件」是两件事；只要脚本从开发机的目录里读东西，就必须有一条门禁问
「干净克隆拿得到吗」。检查的方向也要对称：既要「BUILD.gn 点名的都有文件」，也要
「安装的文件都被跟踪」，因为前者看不见孤儿文件，后者看不见被删掉的引用。

**门禁**：`tools/audit-core-inputs.sh`（无需 Chromium 树，进 CI 的 check job）。
BUILD.gn 点名的每个源必须在本仓库里**唯一**解析到且被 `git ls-files --error-unmatch`
命中；`sync-core.sh` 会安装或应用的每个文件同理。补丁集合由 `sync-core.sh` 自己的
`apply_patch` 行导出，**不是**由 `core/patches/*.patch` 通配导出——后者在删掉一个补丁时
只会安静地少列一个文件。`sync-core.sh` 另外在安装后做反向校验：BUILD.gn 声明的名字必须
在 `$CHROMIUM_SRC/minicronet/` 里真实存在，这一半覆盖安装 glob 到不了的位置。

**扫描范围要按「像什么」而不是按「今天是哪些」来写**。第一版用
`"[A-Za-z0-9_./-]+\.(c|cc|h|def|exports|lds)"` 抽名字，变异测试把 BUILD.gn 里的 `.cc`
全改成 `.c_` 后，抽取结果**静默缩到 3 个名字**（只剩 `.def/.exports/.lds`）而脚本照旧
通过——一个引用只要不再「像文件名」，就从扫描里消失了。现在的写法是两半对称的：

- 抽取用任意扩展名 `"[A-Za-z0-9_./-]+\.[A-Za-z0-9]+"`，排除 GN 的 `//` 导入；
- 扩展名不在安装清单里（例如有人加 `.cpp`）是**硬失败**，要求同时教会门禁和
  `sync-core.sh`，而不是安静跳过；
- 反向不变式兜底：`core/source/` 下的每个 `.c`/`.cc` 都必须被 BUILD.gn 里某个 target
  引用，孤儿文件同样失败。上面那次改名正是被这条抓到的。

变异测试（10 项，全部确认非零退出后恢复）：删 probe 文件、probe 未跟踪、删补丁、
新增未跟踪的 `.cc`、BUILD.gn 引用不存在的源、不是 git 检出、`sync-core.sh` 引用不存在
的补丁、存在未被应用的孤儿补丁、BUILD.gn 引用 `.cpp`、BUILD.gn 里的引用不再像文件名。
前两项漏洞（通配扫补丁目录漏掉「删掉一个补丁」、扩展名白名单漏掉「改名」）**都是变异
测试发现的，不是评审发现的**——门禁自己的盲区只能靠故意破坏来暴露。

**另一层**：后置校验**不是**冗余的。新增一个顶层 `core/source/mutation_top.h` 并在
BUILD.gn 里引用它时，`audit-core-inputs.sh` 通过（名字在仓库里解析得到、也已被跟踪），
而 `sync-core.sh` 在 ninja 之前失败并指名该文件——因为安装 glob 只复制
`core/source/*.{c,cc}` 与 `core/source/minicronet/*.h`。两半各自覆盖对方的盲区，
所以两半都要留。

---

## 25. 兜底工具比没有工具更糟

**现象**：`tools/audit-core-binaries.sh` 校验 8 个平台产物都导出那 20 个 ABI 符号，但它
按格式挑工具的顺序是「Linux 用 `nm`，否则有 `llvm-nm` 就用它，否则退回 `strings`」。
两个方向都错了：

- 把 Chromium 的 `llvm-nm` 放进 `PATH` 后 Windows 目标全部失败：`llvm-nm` 对 PE 镜像
  回答 `no symbols` 并**仍然退出 0**（导出表在 image data directory 里，不在 COFF 符号
  表里），于是每个 DLL 都被判成「没有导出符号」。
- 在没有 `llvm-nm` 的机器上退回 `strings`，而 `strings` 找到的是**字符串字面量**：
  把 x86_64 DLL 的导出目录 RVA/Size 清零（一个符号都不导出）之后，二十个名字仍在
  `.rdata` 里，`strings` 照样报 20/20。**这个检查当时可以整个被绕过而全绿。**

**根因**：把一条「找不到就用弱一点的工具」的降级链写进了**断言**。降级对日志合理，对
门禁不合理：门禁的强度等于它最弱那条路径，而弱路径平时不报错、只在真出问题时给出错误
结论。`strings` 那行注释甚至写着「without pretending to validate a foreign linker
table」——作者知道它不验证，但它仍然让脚本以 0 退出。

**规则**：门禁要么用能给出**权威答案**的方法，要么**失败**。不得存在「主工具缺失时用一
个无法区分真假的方法顶上」的分支。要跨格式就必须真的解析格式，而不是找一个恰好对当前
样本有效的近似。

**门禁**：新增 `tools/core-exports.py`，用标准库直接解析 ELF `.dynsym`、PE 导出目录、
Mach-O 的 dyld export trie（`LC_DYLD_EXPORTS_TRIE` / `LC_DYLD_INFO_ONLY`）。Python 本
就是该审计的硬依赖（它解析 manifest），所以 CI 与开发机跑同一份实现，没有可缺失的外部
工具。解析失败或结果为空都是**硬失败**。选择它而非 `llvm-readobj`，是因为后者不在
Ubuntu runner 上，会让审计在 CI 里直接失败——把「本地能跑」换成「哪里都跑不了」不算修好。

变异测试（3 项，各自刷新 manifest 以越过 SHA-256 那一步）：x86_64 DLL 导出目录清零、
ARM64 DLL 导出目录清零（binutils `objdump` 读不了该架构）、ELF `.dynsym` 段类型改成
`SHT_NULL`。三项都非零退出并指名 `cannot read the export table`。修改前后都用
`nm -D` 与 `llvm-nm` 交叉核对：8 个产物各 20 个 `mn_*`，集合逐元素相同。

**自己写的解析器同样要过变异测试**：第一版 `core-exports.py` 有两个 bug——ELF32 的
`st_info`/`st_shndx` 下标沿用了 ELF64 的布局（32 位 ELF 因此报「没有导出符号」），
Mach-O trie 的子节点偏移按当前节点而非 trie 起点计算（直接读崩）。两者都是**看输出**发现
的：32 位产物返回 0 个符号、Mach-O 抛 `IndexError`。所以解析器的验收标准是「8 个产物各
20 个符号，且与成熟工具逐元素相同」，而不是「脚本没报错」。




## 26. 上游删掉一个 API 时，「适配」必须是行为等价而不是删代码

**现象**：153 → 156.0.8078.31 升级时，`net::SSLContextConfig::ech_enabled`（155 删除）
与 `kAvoidH2Reprioritization`（155 删除）都消失了。第一反应是删掉 Core 里对应的两行。

**根因**：删掉 `config.ech_enabled = profile_.ech_enabled;` 是**安全的**，因为 ECH 策略
由 `GetEchMode()` 每 profile 返回，那一行本来就是冗余副本。但删掉
`spdy_session.cc` 里 `if (avoid_h2_reprioritization_) return;` **不是**等价的：成员变成
「只写不读」，`-Werror -Wunused-private-field` 会直接编译失败（这一步救了命），而如果
关掉那个警告，profile 提供的开关就静默失效了。

**规则**：上游删除某个 feature 时，先查它在**最后一个存在的版本**里的默认值——156 里删掉
的行为，154 里是 `FEATURE_DISABLED_BY_DEFAULT`。若默认关闭，则「保留成员 + 恢复同一个
early return」与「删掉」在**所有 pinned profile** 上等价，且不会把开关静默吃掉。判定依据
必须落到具体行号（`features.cc:37`），不能只说「删了应该没关系」。

## 82. 「默认路径不成立」被写成了「产品边界」

**后续（同一条缺陷的另一半）**：上一段修的是文档。真正让「默认路径不成立」变成产品行为的是
`core/source/engine.cc`：建 resolver 的整个分支被 dns_* 参数包裹，不传参数就落到 builder 的
默认 `ManagerOptions()`，其中 `insecure_dns_mode` 是 `kDisabled`，
于是只跑 `TaskType::SYSTEM`（getaddrinfo），DNS HTTPS 记录永远不被查询。
修法是把 resolver 建成无条件的，并把 `insecure_dns_mode` 设为 `kEnabledBuiltIn`、
`additional_types_via_insecure_dns_enabled` 显式设为 true——这正是桌面 Chrome 的默认配置
（`stub_resolver_config_reader.cc:190` 与 `net/base/features.cc:40-43`）。
dns_* 从「开启开关」降级为「逐字段覆盖」，语义没变，默认行为变了。

**验证方式值得单独记**：外网 DNS 会自己变（同一台机器上换时刻跑，type 65 有时有应答有时没有），
所以「默认路径会不会发 type 65」不能靠连 public 域名断言。可重放的做法是起一个本地应答器，
让它对 type 65 回一条带 `ech=` 的 HTTPS 记录，再用 `dns_nameservers="127.0.0.1:PORT"` 指向它：
这样 `success` 与 `sni=encrypted` 都由本机决定，与外网无关。
用 `strace -f -xx -e trace=sendto` 比对改动前后的 query type 也能直接看到差别：
改动前默认路径只有 `type=1`，改动后是 `type=65` + `type=1`。


**现象**：README、`docs/ABI.md`、`docs/COMPATIBILITY_BOUNDARY.md`、`core/abi/minicronet.h` 与 Python
stub 五处都在断言「本引擎不取 ECHConfigList，所以 `ech_status` 恒为 `not_attempted`」，
并把原因归给「桌面 Chromium 的边界」。实测：`chrome_158` 对 `https://crypto.cloudflare.com/cdn-cgi/trace`
只要传 `dns_nameservers="1.1.1.1"`，`ech_status=success`、响应体 `sni=encrypted`；不传任何
dns_* 参数才 `not_attempted` / `sni=plaintext`。

**根因**：`core/source/engine.cc:801` 的 DNS 覆盖分支是有条件的——只有传了 dns_* 参数才
`set_host_resolver(CreateStandaloneResolver(...))`。不传时 builder 用 Chromium 默认 resolver，
走 `TaskType::SYSTEM`，根本不发 DNS HTTPS 记录查询，`ech_config_list` 恒空，于是 GREASE。
把「默认配置下的观察」写成了「实现上的不可能」，顺手把责任推给了上游。

**为什么五处都错而门禁没红**：没有任何门禁拿「ECH 能不能成功」当断言。`audit-padding-rule.py`
只钉「ECH 起始大版本是 119」，`verify-wire-capture.py` 只钉「119 起每条连接都有扩展且 payload
互不相同」——两条都在**没有 ECHConfigList** 的前提下成立，所以错误断言与它对得上。

**规则**：把结论写成「不可能 / 永远 / 恒为」之前，先问这一句——**是代码里没有这条路径，还是
我这次调用没走进这条路径？** 前者要给出「没有」的证据（哪几行使其不成立，§12 同样要求否定性
结论有证据），后者只是默认值。本次的证据是 `engine.cc:801` 的条件分支 + 一次对照实验
（同一 profile、同一 URL，只改 DNS 参数），而旧断言连一个「哪行阻止了它」都指不出来。

**复核命令（可重放）**：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python python3 - <<'PY'
from chrome_client import Session
URL = "https://crypto.cloudflare.com/cdn-cgi/trace"
for label, kw in (("system", {}),
                  ("dns_nameservers", dict(dns_nameservers="1.1.1.1")),
                  ("doh", dict(doh_template="https://cloudflare-dns.com/dns-query",
                                dns_secure_mode=2))):
    with Session(impersonate="chrome_158", **kw) as s:
        r = s.get(URL, timeout=30)
        sni = [l for l in r.text.splitlines() if l.startswith("sni=")]
        print(label, r.ech_status, sni)
PY
# 本机实测：
#   system          not_attempted  ['sni=plaintext']
#   dns_nameservers success        ['sni=encrypted']
#   doh             success        ['sni=encrypted']
```

源码侧的那一行是 `core/source/engine.cc:801` 的条件分支；上游侧是
`net/socket/ssl_client_socket_impl.cc:1897-1910`（先无条件开 GREASE，再看
`ech_config_list` 是否为空）。**没有 `ech_config_list` 就没有真 ECH，而这个 list 只在引擎
自己建 resolver 时才可能有内容**——两句合起来才是完整结论，旧文档只写了后半句，然后把它
读成了「永不发生」。
**顺带修掉的**：`not_attempted` 现在同时表示「默认 resolver 没查」与「查了但域名没有
HTTPS 记录」两种情形，调用方无法区分。要分开需要新 ABI 值（例如 `MN_ECH_NO_CONFIG`），
那是新增面，不在本轮文档修正范围内——已记入 `docs/NEXT_STEPS.md`。

## 27. 门禁的「方向」也是覆盖面：只查超集的门禁看不见多余的成员

**现象**：同一类盲区在这次审计里出现三次，都是「只验证一个方向」：

1. `tools/generate-profile-table.py` 的边界断言用手写字典逐 profile 列举，表从 151 涨到
   155 时字典没跟着涨——**57 个 profile 里有 27 个从未被任何断言检查过**，其中包含刚加
   的 152–155。断言「通过」和「覆盖到」是两件事。
2. `tools/audit-abi.sh` 用 `for symbol in $symbols; grep` 检查 ABI：header/FFI/Core 各
   多出一个导出时全部通过，还照样打印「20 symbols」。ABI v8 是冻结契约，header 多一个
   就是破坏。
3. `tools/audit-core-binaries.sh` 同样只查超集：8 个产物各多导出一个 `mn_*` 也能全绿。

**规则**：写「集合一致」的检查时，必须同时问两个方向——**少的**（缺了谁）和**多的**（多
了谁）。只查一个方向的门禁会在对象增长时静默失效，而它打印的成功信息还会主动误导。

**门禁**：
- 边界断言改为**区间**+完整性守卫：区间必须恰好铺满 `chrome_99..LAST_PROFILE_MAJOR`，
  留洞直接失败（变异测试：把 `(99,123)/(124,155)` 改成 `(125,155)` → 报
  `uncovered majors: [124]`）。
- 新增 `tools/abi-exports.py`，从 header 反解实际声明的导出集与 v8 清单逐元素比对
  （变异测试：注入第 21 个导出 → 报 `header declares exports that are not part of ABI v8`）。
- `audit-core-binaries.sh` 增加导出名集合的 `cmp` 比对（变异测试：让解析器多报一个名字 →
  报 `exported mn_* set differs from ABI v8`）。

**另一个同源教训**：文档提交号的扫描原先手写列举 9 个文档，仓库里有 22 个 `.md`。改成
`glob("*.md") + glob("docs/**/*.md")` 全量扫描并断言每个文件都被 Git 跟踪（未跟踪即失败，
因为干净克隆检查不了它），覆盖数从 9 涨到 26。**任何「手写文件清单」的门禁都是下一个
漂移点**。

## 28. `git checkout -- <file>` 会静默丢掉未提交的改动

**现象**：为清理一次变异测试的现场，对 `docs/LESSONS.md` 执行了
`git checkout -- docs/LESSONS.md`。该文件里有本次会话刚写、**尚未提交**的两条教训，
连同一起没了。`git status` 显示该文件干净，看不出任何异常。

**根因**：这是「禁止提交」与「用 git 清理现场」两条规则叠加出的陷阱：改动只在工作区时，
`checkout`/`restore` 是**破坏性**操作，而它的输出是沉默的。禁用提交的会话里，工作区就是
唯一的副本。

**规则**：在工作区持有未提交成果时，清理任何文件前先
`cp <file> /tmp/<name>.bak`；`git checkout --`、`git restore`、`git stash` 一律视为删除
操作。需要「只回退某次实验」时，用备份还原而不是用 git 还原。

**教训**：这一条是被自己踩出来的——同一份文件里既记录着「变异测试后要恢复原状」，也
因为恢复手段选错而丢了内容。
## 泄漏门禁的「晚期窗口」只有两个点时，它什么也证明不了

`tools/leak-hunt.py` 的判据分两步：先用 `_floor()` 取相邻采样的最小值去掉单次抖动，
再对**后半段**拟合斜率——前半段是缓存预热，后半段才看得出「是否每个请求都在涨」。

缺陷是这两步的次序：`half = max(2, len(samples) // 2)` 用的是 **floor 之前**的长度，
`5 // 2 = 2`；而 `_floor()` 已经把 6 个采样点变成 5 个，于是 `fds[2:]` 只剩 **3** 个点，
再往下 `xs[2:]` 与 `fds[2:]` 长度不等时更是只剩 **2** 个。两个点永远能精确拟合一条直线，
斜率因此不含任何信息，判据退化成只剩绝对门槛。

后果是**两个方向都错**，而且漏报那一侧更严重：

| 输入（fd 序列） | 应判 | 修复前 | 修复后 |
| --- | --- | --- | --- |
| `9,12,15,18,21,24`（每批 +3，真实泄漏） | 泄漏 | **flat** | `grows:fd` |
| `9,9,9,9,9,12`（末尾开了一条连接） | flat | flat | flat |
| `9,12,15,15,15,15`（缓存填满后收敛） | flat | flat | flat |

**这是打 tag 前那次 review 里 `leak-hunt 12/13` 的真正来源**：门禁自己抖动，而不是被测
对象泄漏。同一份代码、同样的参数连跑 4 次都是 13/13，正是因为失效窗口在真实序列上
只有偶尔才落进「看起来涨」的形状。

修复两条：

1. `half` 按 **floor 之后**的长度算，且 `LATE_WINDOW = 3` 保底——两点拟合没有自由度，
   三点才有。
2. 绝对门槛按每批请求数缩放（`absolute_floors()`）。它是「一个连接 / 一个 arena」的
   量级，而 `--requests 20` 时一个 0.05 fd/请求 的真实泄漏每批只移动 1 个描述符，
   固定门槛 3 会把它整个吃掉。下限仍是 3 / 2048 KiB，所以任何批量下抖动都不够格。

**教训**：一个判据在两次运行里给不同答案时，先怀疑判据，再怀疑被测对象。当时如果直接
把 `12/13` 记成「偶发、可复现不了」就发布，这个门禁会继续在真实泄漏上保持沉默。

回归：`bindings/python/tests/test_leak_verdict.py`（8 条）直接调用 `judge()`，不复述
它的算术——第一版测试自己重写了切片，把缺陷抄了一遍，因此杀不掉变异。把 `half` 改回
旧算式后 `test_the_verdict_does_not_depend_on_the_batch_size` 立刻红（`flat` != `grows:fd`）。

---

## 29. 一个会话里「每次请求都换配置」会悄悄退化成每次新建一条连接

**现象**：代理池跑一段时间后大量 `ERR_PROXY_CONNECTION_FAILED`(-130) 与
`ERR_TUNNEL_CONNECTION_FAILED`(-111)。代理本身是健康的，日志里没有任何配置错误。

**根因**：代理是 Engine 级设置（`engine.py` 的 `EngineConfig` 键含 `proxy`），而一个
Engine 是一整个 Chromium `URLRequestContext`——独立线程、socket 池、DNS 缓存、TLS
会话缓存。会话默认只保留 `max_engines=8` 个（`engine.py:DEFAULT_MAX_ENGINES`），超出
按 LRU 淘汰。**池比 8 大时，几乎每个请求都命中一次淘汰**，于是每个请求重建一个上下文
并新开一条 TCP 连接，连接复用彻底失效。实测（20 端点池、200 请求）：

```
max_engines=8   -> 构建 200 个 Chromium 上下文、代理侧 200 条 TCP、遗留 TIME_WAIT +180
max_engines=20  -> 构建 20 个、20 条 TCP、TIME_WAIT +20
```

每条废弃连接留下一个 TIME_WAIT；临时端口（`32768-60999`）耗尽后 `connect()` 拿不到
源端口，代理路径随即报 -130。同一台机器上，**资源耗尽在代理路径上的表现就是 -130**：
把进程 fd 打到 `EMFILE` 后，走代理的请求报 `ERR_PROXY_CONNECTION_FAILED`，直连请求报
的是另一个码。所以「跑一阵子才炸」不是库变坏了，是连接数线性累积后的资源问题。

**规则**：凡是「按请求切换的配置」，都要先问它是不是**有代价的全局设施**。Engine 就是：
换代理 = 换整个上下文。这类参数必须同时满足两条：文档里出现，且与使用者能数出来的规模
（这里是池大小）挂钩。`max_engines` 此前只写在 `.pyi` 里，README 一次都没提，于是没有
人会想到去调它。

**门禁**：`ProxyRetryPolicyTests` 用 `_send_once` 的调用次数锁定重试语义；
`ProxyURLNormalisationTests` / `ProxyCredentialScopeTests` 覆盖 scheme、URL 形态与凭据
作用域。三组都用变异测试确认过：把归一化改成恒等、把 `socks5h` 别名清空、把
`proxy_auth` 的作用域判断去掉，对应测试各自失败。

## 30. 池里读来的字符串不是 URL，是「看起来像 URL 的字节」

**现象**：同一批代理，直接用能通，从文件/接口读来就整批 `ERR_NO_SUPPORTED_PROXIES`
(-336)。该错误读起来像「没配代理」，实际是「规则解析出空列表」。

**根因**：`ProxyRules::ParseFromString` 对规则形状很严格，而 facade 把调用方字符串
**原样**交给它（`split_proxy_credentials` 在没有 userinfo 时直接 return）。尾随换行、
路径、片段都会让整条规则解析为空。实测这些写法在修复前各自失败：

```
"http://h:p/"      -> -336     "http://h:p#f"      -> -336
"http://h:p/path"  -> -336     "http://h:p\n"      -> -336
"http://h:p?x=1"   -> -105     "http://h:p "       -> 200（被 TrimLWS 吃掉）
```

同一类问题的另一半是 scheme：Chromium 的白名单只有
`http`/`https`/`socks`/`socks4`/`socks5`（`GetSchemeFromUriScheme`），而池子常给
`socks5h://` / `socks4a://`，两者都变成 -336。**但这两个词含义不同**：`socks5h` 只是
拼写（Chromium 的 SOCKS5 本来就把主机名交给代理），`socks4a` 是能力（Chromium 的
SOCKS4 在本地解析，Core 没有远端解析模式）。所以前者改写、后者抛
`UnsupportedFeature`——把后者「顺手支持」等于把用户的 DNS 悄悄改走本机。

**规则**：外部输入在进入严格解析器之前必须归一化，且归一化只能吞掉**确定无意义**的部分；
凡是改变语义的拼写差异，要么证明它与已有行为等价（`socks5h`），要么 fail-closed
（`socks4a`）。这正是 AGENTS.md §8 的承诺在代理路径上的落点。

**门禁**：`ProxyURLNormalisationTests` 逐条锁定上表，并用真实 Core 在
`test_pool_string_shapes_actually_route_through_the_core` 里断言响应体是代理回显的
`proxied:<url>`——所以「直连成功」伪装不了通过。

## 31. 会话级凭据不能跟着逐请求的目标走

**现象**：`Session(proxy=A, proxy_auth=(u, p))` 搭配单次 `proxy=B` 时，**B 收到了 A 的
用户名和密码**。

**根因**：`proxy_auth` 是会话状态，Engine 却按请求选。旧代码在 URL 没有 userinfo 时
无条件套用 `self.proxy_auth`，于是任何一次换代理的请求都会把会话凭据发出去。

这个缺陷**不会自己暴露**：`ProxyHandler` 这类从不挑战的代理收下 `Proxy-Authorization`
就照常 200，泄漏无声。只有让代理**真的回 407** 才看得见——修复前观测到
`Basic cG9vbHVzZXI6cG9vbHBhc3M=` 被发给了无关代理，随后以
`ERR_TOO_MANY_RETRIES`(-375) 结束。

「会话自己的代理」有两个写法：`proxy=` 与 `proxies` 映射。初版只锚定了前者，
于是用 `proxies={'http': A}` 配的会话在单次 `proxy=B` 时**仍然把凭据发给了 B**——
同一个缺陷换了个入口。修完两个入口都要有测试，只修一个等于没修。

**规则**：凭据要绑定到**所有者**（这里是「会话自己配置的那个代理」），不是绑定到
「发出请求的这一次」。首次写测试时我断言的是「会话自己的代理能拿到凭据」——那是正向
路径，永远为真，变异测试立刻证明它抓不到缺陷。改成断言「无关代理收到的
`Proxy-Authorization` 全为 None」后，去掉作用域判断会让测试失败。

**门禁**：`ProxyCredentialScopeTests` 的
`test_a_per_request_proxy_does_not_receive_the_session_credentials` 与
`test_the_session_proxies_mapping_also_scopes_the_credentials`，两个入口各一条，
都用真实 407 挑战夹具记录实际收到的头。

## 32. 错误穿过一层「更安全」的类型时，会丢掉调用方唯一能用的字段

**现象**：同一个 -111 隧道失败，`allow_redirects=True` 报
`ERR_TUNNEL_CONNECTION_FAILED (net error -111)`，而 `allow_redirects=False`
（`Session.head()` 也走这条路）只报一个裸的 `Proxy`。用户看到的
`Proxy (Chromium net error -111)` 就是这个形态。

**根因**：Rust 层有两个错误类型。`RequestError` 有 `.error`（粗类别）和
`.net_error`（Chromium 数字码），`Display` 会拼出 `Proxy (Chromium net error -111)`；
而 `Error` 只有类别，`Display` 就是 `Proxy`。

```
crates/minicronet/src/request.rs:366
  pub fn wait_for_redirect(&self, timeout: Duration)
      -> Result<Option<Redirect>, Error>        // ← 返回窄类型
crates/minicronet/src/request.rs:384
  } else if let Some(Err(error)) = state.terminal {
      Err(error.error)                          // ← 拆包，丢掉 net_error
```

`wait_manual` 先看 `try_take()`（`ResponseFuture`，返回 `RequestError`，正常），超时切片
里再调 `wait_for_redirect`（返回 `Error`，已丢码）。同一段循环里两条分支的粒度不同，
所以只有「先等到 redirect 事件」那条路会掉字段。

**规则**：错误类型在**所有**返回路径上必须携带同样多的信息。「更窄的返回类型更安全」
是错觉——它把调用方唯一能据以行动的字段（数字码）删掉了。收缩错误类型前先问：调用方
拿这个类别能做什么？这里答案是「什么都做不了」，因为类别名恰好是 `Proxy`。

**门禁**：`wait_for_redirect_preserves_the_net_error`（Rust，断言
`Display` 含 `-111`）+ `ManualRedirectErrorNamingTests`（Python，断言
`head` / `allow_redirects=False` / 跟随三种模式都以 `ERR_` 开头且含 `net error`）。
变异测试：把 `Err(error)` 改回 `Err(RequestError { error: error.error, net_error: 0 })`，
两条 Python 测试立刻失败。

## 33. 一个错误码把多件事折叠成一件时，文档要写清它折叠了什么

**现象**：用户报告「大量 `-111`」，直觉是隧道实现有问题。

**根因**（上游行为，非缺陷）：`http_proxy_client_socket.cc:521-530` 的
`DoProcessResponseCode` 只对 `200` 与 `407` 分别处理，**其余状态码一律**
`ERR_TUNNEL_CONNECTION_FAILED`。源码注释写明了原因——防代理冒充源站。实测确认
403/404/405/429/500/502/503/504/301/400 全部报同一个 -111。

所以「大量 -111」的真实含义可能是：代理在限流（429）、代理不让你去这个目标（403）、
代理自己上游挂了（502/503），三者对调用方是同一个字符串。

**规则**：上游把多个原因折叠成一个码时，**折叠的事实本身**必须进兼容边界文档。用户会
把这个码当成诊断结论反复误判，而库这边无法拆开它（拆开就要改 Chromium）。能给的只有
「这个码不区分 X/Y/Z，去查代理日志」。

**门禁**：无自动化门禁（这是上游语义）；已写入
`docs/COMPATIBILITY_BOUNDARY.md` 的 Proxies 一节，并附 10 个状态码的实测结果。

## 34. 「修好了」必须由独立的对抗性审计确认，不能由修它的人确认

**现象**：代理三处修复（scheme 归一化、凭据作用域、`-111` 命名）当时逐条验证过，
测试也全绿。但要求「审计排查」时，实测又查出 **3 个真实缺陷**，其中 2 个是上一轮
自己引入的。

**漏掉的三个**：

1. **凭据作用域只锚定了半个入口**。会话用它自己的 `proxy=` / `proxies` 声明代理，
   **单次调用的** `proxy=` / `proxies=` 是另一回事。上一轮修完两个会话入口后，
   单次 `proxies={B}` 仍会把会话凭据发给 B（实测 B 收到 `Basic ...` 后 `-375`）。
2. **`:0` 被当成假值丢掉**。`authority = ... if port else host` 让 `http://h:0`
   变成 `http://h`，而 Chromium 的 `ParsePort("0")` 返回的是**真的 0**，不是方案
   默认端口。于是「连端口 0」被静默改成「连端口 80」。
3. **空端口被静默补成默认端口**。`http://h:` 是 Chromium 明确**拒绝**的规则
   （`ProxySchemeHostAndPortToProxyServer`：端口存在但为空 → `ProxyServer()`），
   归一化却把它读成 `http://h`（端口 80）——一个 Chromium 会拒绝的输入变成了能
   发出去的请求。同一个假值判断还让 `http://h:8080 # rot=2` 报 invalid port，
   把用户指向了字符串里错误的位置。

**为什么自己的测试没抓到**：三条测试都是**正向**的（凭据能到位、合法 URL 能解析），
没有一条断言**不该发生的事**。变异测试当时只覆盖了我改动的行，没覆盖我**没改**的
相邻分支——`:0` 和 `:` 恰好都落在同一条 `if port` 里。

**规则**：审计要按**输入空间**穷举，不是按「我改过的行」复查。具体做法：

- 拿上游语义当**判据**（这里逐条对照 `ParsePort` / `GetDefaultPortForScheme` /
  `ProxySchemeHostAndPortToProxyServer`），而不是拿「我当时的意图」当判据；
- 每个假值/空值分支都要有一个用例（`0`、空串、`None`、缺省）；
- 每个「作用域」概念都要把**所有**入口列出来，缺一个就等于没修。

**门禁**：`ProxyURLPortTests`（`:0` 保留、`:` 拒绝、尾随文本、幂等性）+
`ProxyCredentialScopeTests` 补到 4 个入口。三条新变异（假值端口、空端口放行、
把单次代理当凭据所有者）各自都能让对应测试失败，已逐个确认。

## 35. 扩展层必须长在原生之上，而不是替代原生

**现象**：为了让代理池的脏字符串能用，我写了一个归一化层，把任何合法输入都改写成
`scheme://host:port`。结果是**把 Chromium 本来就能用的配置改坏了**。

**根因**：`ProxyRules::ParseFromString` 支持三种形式，归一化只认识其中一种：

```
"http=h:p"                    -> 原样变成 "http://http=h:p"  （host 叫 "http"）
"http=h:p;https=h:p"          -> 变成 "http://[http=h:p;https=h:p]:8080"
"http://a:p,http://b:p"       -> InvalidProxyURL（把逗号当成尾随文本）
"direct://"                   -> InvalidProxyURL（当成未知 scheme）
```

`;` 和 `=` 是 `ParseFromString` 的 tokenizer 分隔符，`,` 是 `AddProxyURIListToProxyList`
的回退列表分隔符，`direct://` 是 `MultiProxyUrisToProxyChain` 认的"不走代理"记号。
我的归一化把这些全当成了脏数据。

**实测确认**（绕过 facade 直接喂 Engine）：
`http=h:p` → 200；`http=h:p;https=h:p` → 200；`a,b` → 200 且第一条挂了会自动用第二条；
`direct://` → 不走代理。四种在修复前全部被 facade 弄坏。

**规则**：写兼容层时，先穷举上游**接受**什么，再决定要改什么。默认动作必须是
「原样透传」，扩展只处理上游**明确拒绝**的输入（这里是 `socks5h`、脏尾字符、
裸 `host:port`）。判断顺序固定为：能原生解析 → 不改；不能 → 扩展或 fail-closed。
把「我认识的形式」当成「唯一的正确形式」是这类缺陷的共同来源。

**门禁**：`NativeProxyRuleTests` 8 条，逐条锁定上表，其中一条用真实 Core 断言
`http=h:p` 与 `a,b` 的响应体是代理回显的 `proxied:<url>`，所以"只是字符串没变、
实际没走代理"伪装不过。变异测试：关掉透传分支后 3 条失败，放行 `direct://` 混用后 1 条失败。

## 36. 三层各自的「我以为已经对了」

**背景**：要求按 Core / Rust / Python 三层分别审计。查出的不是协议实现问题，而是**每一层
对「同一个事实」各记了一份，且互不校验**。

**缺陷 1（Core）**：`core/source/minicronet/error_mapping.h` 的代理分支漏了四个上游码——
`ERR_MANDATORY_PROXY_CONFIGURATION_FAILED`(-131)、`ERR_UNEXPECTED_PROXY_AUTH`(-323)、
`ERR_UNABLE_TO_REUSE_CONNECTION_FOR_PROXY_AUTH`(-170)、`ERR_NO_SUPPORTED_PROXIES`(-336)。
它们落到 `default` 分支变成 `MN_ERROR_NETWORK`。实测修复前 `proxy=" ; "` 报
`Network (Chromium net error -336)`，修复后报 `Proxy (net error -336)`。

**缺陷 2（Rust 绑定）**：两个版本的 `duration()` 都用 `Duration::from_secs_f64`，它在超过
`u64::MAX` 秒时**panic**。pyo3 把它转成 `PanicException`，而它继承 `BaseException`——
**穿透所有 `except Exception`**，直接杀进程。实测 `timeout=1e20` 触发，`timeout=1e19` 正常。

**缺陷 3（Rust 绑定一致性）**：`bindings/python36` 有 15 处 `lock().unwrap()`，而
`bindings/python` 全部用 `lock().unwrap_or_else(PoisonError::into_inner)`。一次 panic 会让
那个 mutex 永久中毒，之后每次加锁都再 panic——同一个进程再也发不出请求。

**缺陷 4（Python facade）**：`-336` 在 facade 表里本来就有，但**只有这一处记得它**。
表格靠 `_NATIVE_ERRORS` 的子串匹配决定异常类，Core 说 `Network` 时不是 `ProxyError`；
而表里没登记的 `-170/-323/-364/-366` 完全靠 Core 的类别，于是变成 `ConnectionError`。

**规则**：同一份「协议事实」被三层各存一份时，三层必须有同样的覆盖面，并且**由测试
同时断言三者**。只测一层会得到「测试全绿但用户看到 ConnectionError」的结果。

**门禁**：(1) `cargo build` 编译 Core 后 `audit-net-error-names.py` 仍然逐行核对；
(2) `ProxyErrorClassificationTests` 用 14 个码 × 2 种类别断言都得到 `ProxyError`；
(3) `TimeoutBoundaryTests` 断言 `1e20/1e300/inf/nan/-1` 全部**抛异常而不是 panic**，
并显式 `assertNotIsInstance(..., KeyboardInterrupt)` 以保证不是 `BaseException` 逃逸。
变异测试：删掉 5 行新表格 → 2 条失败；关掉分隔符守卫 → 1 条失败。

## 37. 声明支持的版本，必须由「最老的那一版」来验证

**问题**：`requires-python = ">=3.6,<3.14"`，扩展用 `abi3-py36` 编译，CI 也真的在
3.6 上装过 wheel——但**纯 Python 那一半从没在 3.6 上跑过**。

**缺陷 1**：`_python_impl/models.py` 直接调用 `asyncio.get_running_loop()`，那是 3.7+。
在 3.6 上抛 `AttributeError`，而周围的守卫只写了 `except RuntimeError`——
`AttributeError` 直接穿过去。实测（摘掉该属性模拟 3.6）：

```
resp.close() -> AttributeError: module "asyncio" has no attribute "get_running_loop"
```

同一个文件里那行 `reader.aclose(cancel=True)` 已经建好了协程，异常逃逸后它**永远不被
await**，于是 native request、socket 和缓冲的 body 一起泄漏。**一处兼容性错误同时是
一处资源泄漏。**

**缺陷 2**（与 3.6 无关，3.12 上同样存在）：`asyncio.run_coroutine_threadsafe` 的目标
循环**没在运行**时，它照样返回一个 Future，但那个协程永远不会被调度。于是
`response.close()` 在「循环已停止」的路径上静默不释放任何东西。

**修复**：`_AsyncBodyReader` 增加 `release_without_loop()`——`state.finish()` 本来就是全
同步的（取消 Core 请求、摘掉回调、清空缓冲），不需要 await。释放路径改成：在同一循环里
→ `create_task`；循环在别的线程运行 → `run_coroutine_threadsafe`；**循环存在但没运行**
→ 直接同步释放。

**规则**：
- 支持范围的下界必须有一条**真正在那一版解释器上执行**的门禁。只做「wheel 能装」是
  不够的：装得上、导得进、但调用即崩的版本，比不支持还糟。
- `try/except` 的异常类型要和**失败模式**匹配。`get_running_loop()` 在 3.6 上不是
  `RuntimeError`，是 `AttributeError`；写错一个 except 就吞不掉。
- 把「异步关闭」当成需要循环的动作之前，先确认它**真的**需要循环。这里三步全是同步
  调用，协程只是语法糖。

**门禁**：`Python36SyntaxFloorTests` 用 `ast.parse(..., feature_version=(3, 6))` 解析
全部 18 个 `*.py`，并用 AST 扫描 3.7+ 的 stdlib 属性调用、要求每一处都在
`hasattr`/`except AttributeError` 守卫内；`AsyncReleaseWithoutLoopTests` 在**循环关闭后**
调 `close()` 并断言不产生 `RuntimeWarning`（unawaited coroutine）。
变异测试：把守卫改回裸 `get_running_loop()` → 语法/守卫测试失败。

## 38. 「装不上」与「没测过」是两件事，支持的边界要写清是哪一种

**背景**：需要回答四个关于版本边界的问题——py36 分支支持 3.4/3.5 吗？主分支支持 3.14/
3.15 吗？这两个版本的 free-threaded 构建呢？

**答案的四个依据**（都可复核）。下面三行的「装不上」结论是**当时（PyO3 0.28.3、
`requires-python ">=3.6,<3.14"`）的边界**；0.30.1 之后上界已经抬到 3.15，见第 39 条。
行的形式保留，因为要说明的是「三种状态不能混说」这个方法，而不是那几个数字：

| 问题 | 结论 | 决定性依据 |
| --- | --- | --- |
| py36 支持 3.4/3.5 | **不可能**（今天仍然成立） | `pyo3-build-config-0.15.2/src/impl_.rs:21` `MINIMUM_SUPPORTED_VERSION = 3.6`；pyo3 0.15.1 的 feature 表里根本没有 `abi3-py35`/`abi3-py34` |
| 主分支支持 3.14 | 当时**装不上**；现在可装（`>=3.6,<3.16` + `abi3-py39`，覆盖 3.9–3.15） | 当时两个 `pyproject.toml` 都写 `requires-python = ">=3.6,<3.14"`，pip 直接拒绝 |
| 主分支支持 3.15 | 当时**装不上**；现在可装（同上） | 当时同上；且 0.28.3 的 `ABI3_MAX_MINOR = 14`（`impl_.rs:43`），abi3 的版本上限就是 3.14。0.30.1 的 `STABLE_ABI_MAX_MINOR = 15`（`impl_.rs:40`）——**上界同样是上游的事实**，换版本才会变 |
| 3.14/3.15 free-threaded | **不承诺，且 abi3 在 free-threaded 上不生效**（今天仍然成立） | `impl_.rs:193` `if self.abi3 && !self.is_free_threaded()`；`impl_.rs:730` 明确写「free-threaded build 不支持 abi3」 |

**容易说错的地方**：

1. **abi3 与 free-threaded 是互斥的**。任何 `abi3-py3N`（当时是 `abi3-py37`，现在是
   `abi3-py39`）在 3.13t/3.14t 上都不会生成 limited API，
   所以「一个 abi3 wheel 顺带覆盖 free-threaded」是错的——必须为 `3.14t` 单独出 wheel，
   且 hint 是 `cp314t` 而不是 `cp314`。
2. **`ABI3_MAX_MINOR` 是硬上限**。pyo3 0.28.3 的上限是 14（0.30.1 把
   `STABLE_ABI_MAX_MINOR` 抬到 15）；超出上限的解释器上它只会 `warn!` 然后回落
   （`impl_.rs:751`），不是「自动支持」。
3. **模块声明了 `gil_used = true`**（`bindings/python/src/lib.rs:846`）。即使装了
   free-threaded 解释器，这个扩展也会被强制重新启用 GIL——「能导入」不等于「并行」。
   代码注释写的是「在专门的 3.13t 测试矩阵出现前保留 GIL」，这是一条有意的选择，不是疏漏。

**规则**：回答「支持到哪个版本」时，必须区分三种状态：**装不上**（元数据拒绝）、
**能装但没测**（风险最高，本仓库 3.6 就长期处在这里）、**测过**。三者不能混说成
「支持」或「不支持」。

**门禁**：`tools/audit-pypi-metadata.sh` 校验 `requires-python`、classifiers 与 README
徽章三者一致；`docs/PYTHON_BINDING_AUDIT.md` 把「free-threaded 3.13t 明确不承诺」
写进表格而不是留给读者猜。

## 39. abi3 的下界是 PyO3 的事实，不是打包选择

**背景**：把 PyO3 从 0.28.3 升到 0.30.1 之后，需要重新回答「这个 wheel 从哪一版
Python 起装得上」。当时文档、徽章、CI 都还写着 3.7–3.13，而新 crate 已经装不到 3.7 了。

**现象**：0.30.1 的最低支持版本是 **3.9**——`pyo3-build-config-0.30.1/src/impl_.rs:26`
`MINIMUM_SUPPORTED_VERSION: PythonVersion { major: 3, minor: 9 }`。它的 feature 表里
abi3 只有 `abi3-py39` 到 `abi3-py315`，**没有** `abi3-py37`/`abi3-py38`（写上去是
编译期错误，不是运行期降级）。abi3 下界决定 wheel 标签，所以打进 PyPI 的是
`cp39-abi3`：本机 maturin 1.15.0 实跑得到 `tagprobe-0.0.0-cp39-abi3-manylinux_2_38_x86_64.whl`。
pip 对 `cp39-abi3` 的处理是**拒绝安装**，不是「装了再 ImportError」——3.7/3.8 用户
看到的是「找不到匹配的发行版」。反方向同样被挡住：用 3.7 解释器配 `abi3-py39` 会让
pyo3 直接报 "cannot set a minimum Python version 3.9 higher than the interpreter
version 3.7"（同文件 `:82-89` 的 `ensure!(min_version <= version, ...)`）。

**根因**：facade 里那句 `sys.version_info < (3, M)` 和扩展的 abi3 下界 N 是**两个
互相独立的数字**，分别活在两处不同的文件里，谁也不知道谁：

- `bindings/python/chrome_client/_python_impl/_native.py` 的 `MODULE_NAME` 行决定
  某个解释器去 import 哪个原生模块；
- `bindings/python/Cargo.toml` 的 `abi3-py3N` 决定那个模块的 wheel 能在哪一版上装。

pyo3 只校验 N 与**构建解释器**的关系，从不读 facade。于是 M 可以停在 3.7 而 N 已经是
3.9，形式化地写成「3.7 的用户装 `cp36-abi3` wheel，然后 facade 去找
`chrome_client_native`」——那个 .so 只在现代轮子里。结果是 **ImportError，且发生在
一个 README 打着 ✓ 的版本上**。这类矛盾不会自己暴露：CI 在 3.10 上跑，本地只有
3.12，两边都离边界很远。

**规则**：

- 「哪个解释器加载哪个扩展」的阈值 **必须等于** 那个扩展的 abi3 下界。两者是同一个
  事实的两种写法，不是两个可以各自调的产品决策。
- 这条等式必须由**门禁断言**，不能靠注释说明。注释在这里特别没用——旧注释把
  「both abi3 wheels cover 3.6-3.13」写得很清楚，而代码早就不是那样了。
- 升级 PyO3 时要顺着问三件事：`MINIMUM_SUPPORTED_VERSION` 还是不是原来那个值、
  `abi3-py3N` 这个 feature 还存不存在、facade 的阈值跟没跟上。三个里错一个都是上面
  那个 ImportError。
- 下界是**上游**的事实。想支持更老的 Python 只能换 pyo3 版本（本仓库就是这么做的：
  0.15.1 的 `ABI3_MAX_MINOR = 9`、`abi3-py36`，覆盖 3.6–3.8），不能靠改 feature 名
  把一个不存在的 abi3 目标「配」出来。

**门禁**：`tools/audit-pypi-metadata.sh` 末尾新增一段，直接从源码解析两边再比：

- 从 `bindings/python/Cargo.toml` 的 pyo3 行取 `abi3-py3N` → N=9；
- 从 `_native.py` 的 `MODULE_NAME` 行取 `sys.version_info < (3, M)` → M=9；
- 断言 `N == M`，不等就报出两个数字与两处 file:line；
- 断言 `bindings/python36/Cargo.toml` 仍是 `=0.15.1` + `abi3-py36`（3.6 侧不许漂）；
- 断言现代 crate 的 abi3 feature 不是 `abi3-py37`/`abi3-py38`（0.30.1 里不存在，
  但它是「想把下界改回去」时最容易写上的值，所以单列一条诊断）。

解析上有一个坑值得记下来：`"abi3-py39"` 里的 39 是「3.9」不是「3.39」。第一版正则写成
`abi3-py(\d+)`，抓到 `36`/`39`，于是 py36 那侧报出 "abi3 floor is 3.36"。现在用
`abi3-py3(\d+)`：既能把 `abi3-py36`/`abi3-py39` 读成 6/9，也能正确处理
`abi3-py315` → 15。

变异测试：把 `_native.py` 的 `(3, 9)` 改回 `(3, 7)` → 门禁 exit 1，报
「declares abi3-py39 (floor 3.9) but ... switches at (3, 7)」；把 `abi3-py39` 改成
`abi3-py38` → exit 1，命中「abi3-py38 does not exist in pyo3 0.30.1」那条。两处都恢复后
门禁重新 exit 0。

**另一半**：光有静态等式还不够，因为等式只证明「两个数字相同」，不证明「那一版真的
能跑」。所以 `.github/workflows/build-wheels.yml` 增加 `python39-smoke` job：在真
Python 3.9 上装 Linux x86_64 现代 wheel，断言
`chrome_client._python_impl._native.MODULE_NAME == 'chrome_client_native'`，然后真发
一次 `https://example.com` 请求并断言 200。这就是第 37 条规则的落点——下界必须有
一条真在那一版解释器上执行的门禁。

---

## 40. 委派出去的检查，读错一个流就会永远为真

**现象**：`tools/audit-python-stubs.py` 里有两个「把判定委派给另一个工具」的检查，
一个读 `stderr`、一个读 `stdout`。把 `tools/expand-verb-signatures.py` 换成
「只往 stderr 写 traceback 然后 exit 1」，审计仍然打印 `0 error(s), 0 warning(s)`
并以 **rc=0** 退出；把 `tools/generate-python-stubs.py` 换成「只往 stdout 写
traceback 然后 exit 1」，同样 rc=0。两个方向都实测复现。

**根因**：两个子工具恰好各写一个流——generator 写 `stderr`（`--check` 的
`stale: ...`），expander 写 `stdout`。审计各自只读了自己预期的那一个，于是
**另一个流里的任何内容都不存在**。更严重的是第二个缺陷：`returncode != 0` 时
代码只做「逐行匹配已知前缀」，**一行都没匹配上时一条 error 都不追加**——退出码
被读到了，却被丢弃了。于是「解析失败」等价于「通过」，这正是 AGENTS.md 第 6 节
禁止的「永远为真的判断」的一种变体：不是条件恒真，而是**失败路径静默产出空集**。

**规则**：

- 委派检查必须**同时读两个流**。子工具往哪个流写是实现细节，审计不该赌。
- `returncode != 0` 且两个流都没解析出任何已知前缀时，必须追加一条**兜底
  error**，带上子工具的 returncode 与输出尾部。退出码本身就是证据，不能因为
  「输出看不懂」而作废。
- 已知前缀的匹配要 `strip()` 之后再比。expander 的 `stale: get (api.pyi)` 前面有
  两个空格（它在分组标题下缩进），第一版用 `line.startswith("stale: ")` 永远匹配
  不上，结果真正过期的签名只能靠兜底 error 报出来——能红，但丢掉了具体信息。

**门禁**：`tools/audit-python-stubs.py` 新增 `parse_delegated_output()` 与
`delegation_failure()`，两个检查共用同一段解析逻辑（单一定义，不再各写一份）。

变异测试（两个方向都必须做，只做一个会漏掉另一个流的假设）：

```bash
# 方向 A：expander 只写 stderr + exit 1
printf 'import sys\nsys.stderr.write("Traceback...\\n")\nsys.exit(1)\n' > tools/expand-verb-signatures.py
python3 tools/audit-python-stubs.py    # 修复前 rc=0；修复后 rc=1
# 方向 B：generator 只写 stdout + exit 1
printf 'import sys\nprint("Traceback...")\nsys.exit(1)\n' > tools/generate-python-stubs.py
python3 tools/audit-python-stubs.py    # 修复前 rc=0；修复后 rc=1
```

修复后两条都 rc=1，并打印
`--check exited 1 but its output matched no known prefix, so the check could not be evaluated; that is a failure, not a pass.`

---

## 41. stub 单侧多出的成员是谎言，不是警告

**现象**：往 `_python_impl/sessions.pyi` 注入一个运行时不存在的方法
`phantom_probe_method`，审计只打印一行 `warning: ... has no runtime counterpart`，
**rc=0**。也就是说门禁看见了缺陷、说出来了，然后放行。

**根因**：`check_module()` 把「stub 有、运行时没有」归到 `warnings`，而
`main()` 只在 `errors` 非空时返回 1。方向是不对称的：运行时多出的成员是
error（`is not declared`），stub 多出的成员是 warning。可这两件事对调用方的伤害
完全相同——只是方向相反：**stub 说有、运行时没有，调用方写下的代码会在运行期
拿到 AttributeError**，而类型检查器全程绿灯。这正是 fail-closed 要禁止的
「对调用方的谎言」，不是提示。

**规则**：stub 单侧多出的 public 成员一律 **error**。唯一的豁免是
`_python_impl/*.pyi` 里的 `X: TypeAlias = ...`——类型别名按定义就不是运行期
值，对检查器是「类型」不是「值」，它不存在于运行时是**正确**的，不是缺陷。
豁免必须窄：只认显式标注了 `TypeAlias` 的声明，不能推广成「凡名字对不上都放过」。

**门禁**：`check_module()` 的 `warnings.append` 改为 `errors.append`，消息里
写明后果（`the stub promises an attribute that raises AttributeError`）。

变异测试：注入 `phantom_probe_method` → 修复前 rc=0（仅 warning）；修复后 rc=1，
`error: _python_impl/sessions.pyi: Session.phantom_probe_method has no runtime counterpart`。
恢复后 rc=0。

---

## 42. facade 重导出的名字，运行期必须真的绑定了

**现象**：`bindings/python/chrome_client/<mod>.pyi` 里有 21 个名字从
`._python_impl.<mod>` 重导出，但运行时那个模块根本不绑定它们。实测：

```
from chrome_client.models import RawHeaderBlock
# ImportError: cannot import name 'RawHeaderBlock' from 'chrome_client._python_impl.models'
```

而这 21 个名字**全部**是 `_python_impl/*.pyi` 里的 `X: TypeAlias = ...` 声明，
所以它们不是缺陷——类型别名重导出给检查器是标准 PEP 484 用法。真正的问题是
**没有任何门禁区分这两类**：`check_facade()` 只比「facade 是否包含 stub 里
列的名字」，两边同源于同一份 `.pyi`，于是它永远自洽。一个**运行时真的被删掉**
的 public 名字会原样通过。

**根因**：facade 的期望集合取自 `_python_impl/<mod>.pyi`（声明），而被检查的
对象也是声明。拿声明去校验声明，等于没校验运行期。AGENTS.md 第 6 节问的
「还有哪些同类载体没被扫到」，这里就是：`_python_impl/<mod>.py`（真正的运行期
绑定）从未进入这条比较。

**规则**：

- facade 里每个 `from ._python_impl.X import Y` 的 Y，必须能在
  `_python_impl/X.py` 里找到绑定；找不到就是 error，因为运行期那句 import 会
  抛 ImportError。
- 唯一豁免：`Y: TypeAlias = ...`。判定用 `ast` 看注解是否以 `TypeAlias` 结尾，
  不靠名单，避免豁免随文件增删而腐化。
- 用 `ast` 解析而不是 `import`：导入这个包需要 Core 共享库在 loader path 上，
  门禁不该有这种运行期依赖。

**门禁**：`tools/audit-python-stubs.py` 新增 `check_facade_runtime_names()`，
配 `runtime_names()`（遍历 `if`/`try` 体内的定义，因为它们也是真实绑定）与
`type_alias_names()`。实测扫描 364 条重导出，42 条 TypeAlias 豁免，0 error。

变异测试（正向：必须报错；反向：必须不报错。只做正向会漏掉「豁免过宽」）：

- 注入运行期不存在的 `PhantomProbe` 到 `models.pyi` → rc=1，
  `re-exports PhantomProbe, but _python_impl/models.py does not bind it`；
- 从 `_python_impl/sessions.py` 删掉真实的 `def merge_setting` → rc=1，
  同时命中 `sessions.pyi` 与 `requests/sessions.pyi` 两条；
- 把豁免临时改成 `if False:` → 21 个真实 TypeAlias 全部浮现（42 条，两个
  facade 各一份），证明这项检查不是空转；
- **反向**：新增一个 `BrandNewAlias: TypeAlias = int` 并让 facade 重导出它 →
  rc=0（TypeAlias 合法地不存在于运行期）；把同一个名字改写成不带 `TypeAlias` 的
  `BrandNewAlias = int` → rc=1。这一对证明豁免**只**认显式的 TypeAlias 声明，
  没有退化成「凡名字对不上都放过」。

**一个被证伪的假设（留档，勿重犯）**：这条缺陷最初被描述成「facade stub 谎报了
22 个可导入的名字」，并打算用 `if TYPE_CHECKING:` 包住这些重导出来「修复」。实测
这个做法是**纯 no-op，而且会打破门禁**：

1. `.pyi` 在运行期从不被执行。把 `models.pyi` 里 `RawHeaderBlock` 那几行**整行
   删除**，运行期行为逐字节不变——`hasattr` 依旧 `False`，
   `ImportError: cannot import name 'RawHeaderBlock' from
   chrome_client._python_impl.models` 依旧来自 `_python_impl/models.py`。
   谎报的载体是运行期模块，不是 stub。
2. PEP 484 规定检查器在 `.pyi` 中把 `TYPE_CHECKING` 视为 `True`，所以包起来对
   检查器也是恒真。两个方向都没有任何变化。
3. facade `.pyi` 是 `tools/generate-python-stubs.py` 的生成物
   （`regenerate_facades()`），手改必然让 `--check` 报 stale，等于让
   `audit-python-stubs` 永久红。
4. 名单本身也是错的：是 **21** 个不是 22 个，且 `cookies` 不属于这 9 个模块——
   `cookies.Cookie` 在运行期真实存在（`cookies.py:25` 的
   `from http.cookiejar import Cookie`），实测 `from chrome_client.cookies import
   Cookie` → `<class 'http.cookiejar.Cookie'>`。

结论：真正的缺陷类别是「**既不是 TypeAlias、运行期又不存在**」，不是「stub 里有
运行时没有的名字」。改门禁前先分清这两类，否则会去修一个不存在的 bug，还会顺手
把生成物改坏。


---

## 43. 用一个"更干净"的默认值换来的，是一次上下文重建

**现象**：`discard_cookies=True` 的语义是"既不发送也不记录 cookie"。实测原始缺陷：

```
session.get(/set-a)                          # 服务端 Set-Cookie: sid=AAA
session.get(/set-b, discard_cookies=True)    # 丢弃成功，但服务端回了 sid=BBB
session.get(/plain)                          # 仍然带 Cookie: sid=BBB   <-- 错
```

最后那次普通请求本该只带 `sid=AAA`，实际带的是 `sid=BBB`——discard 请求**自己的**
响应把 cookie 写进了整个会话后续都在用的 store。也就是说这个"丢弃"请求反过来污染了
它本该隔离的东西。

**根因**：ABI v8 **没有任何入口能清空 Core 的 CookieMonster**（`core/abi/minicronet.h`
里 grep `cookie` 一条都没有）。所以"用一个空 store"只能靠换 Engine 实现——而
**一个 Engine 就是一整个 Chromium `URLRequestContext`**：独立线程、socket 池、
DNS 缓存、TLS 会话缓存。

代价先摆在这里：**每个 discard 请求都多一次完整上下文构建**。这不是可以优化的实现
细节，是 ABI 缺一个 clear 入口的必然结果。

真正的缺陷出在"怎么拿这个一次性 Engine"。原实现靠 `self._generation += 1` 换新
generation——而 `self._generation` 正是**之后每个普通请求读的那个键**。于是那个
本应一次性的 store，变成了全会话的默认 store，正好制造出它要防止的污染。

**规则**：

- 一次性 Engine 的键必须**独立于** `self._generation`。当前实现用负数计数器
  `self._discard_serial`（`generation=-self._discard_serial`），负数与只从 0 增长的
  `_generation` 不可能相撞。
- 用完立即从 `EngineCache` 摘除（`self._engines.discard(one_shot)`），绝不写回
  `self._generation`。不摘除有两个后果：它会成为后续普通请求的默认 store（原缺陷），
  以及它会挤掉本该被复用的普通 Engine（有界缓存）。
- 也不能复用上一次的 discard Engine：它的 store 已经被上一次 discard 响应填过，
  复用就会把上一次的 cookie 泄漏进这一次。每次都要新的空 store。
- Engine 本身要靠 `EngineSlot` 保活（流式响应下还有 native request 持有 Chromium
  上下文的引用），"从缓存摘除"不等于"立刻销毁"。

**门禁**：回归测试覆盖上面那段三步序列——discard 之后紧接的普通请求**不得**带上
discard 响应里的 cookie，且 discard 请求本身既不发送也不记录。这条必须断言正确行为，
不能把"带了 sid=BBB"冻结成期望（AGENTS.md 第 7 节）。

**记在账上的代价**：`discard_cookies=True` 比普通请求贵一次上下文构建。这是
fail-closed 的正当成本——宁可多建一个上下文，也不能假装丢弃了 cookie 而实际把
它们带进下一个请求。等 ABI 增加清空 cookie store 的入口后，这条代价才能消掉；
在那之前它应当写在文档里，而不是藏起来。


---

## 44. 一个只在 Linux 跑过的门禁，在别的平台上可能一行都没读

`tools/audit-network-featurelist.sh` 从 `obj/net/net.ninja` 里按正则抓源文件清单，
写的是 `build obj/net/\S+\.o: ...`。Linux 与 macOS 的目标文件叫 `.o`，Windows 叫
`.obj`。于是这个扫描在 Windows 上匹配到 **0 个源文件**，接着在判据处失败——但报错是
`unknown release-graph FeatureList/FeatureParam/field-trial read: sources=0 reads=0`，
读起来像「源码树变了」，而不是「这台平台的产物根本没有被读到」。

真正让它长期没被发现的是**调用位置**：这个脚本只被 `audit-core-linux.sh` 调用，
Windows/macOS 的审计从来不跑它。所以它「一直是通过的」——只是从来没有在那些平台上
执行过。这和 lesson 6「配置写了不等于生效」是同一件事的另一种形态：**门禁被调用
不等于门禁覆盖面成立**。

**修的时候发现第二层问题**：光把后缀改成 `.o|.obj` 还不够。`net/BUILD.gn` 按平台选源
文件，三个平台族的清单本来就不一样：Linux 534 / Windows 530 / macOS 542，冻结的全局
feature 读取点分别是 175 / 180 / 178。脚本里原本硬编码一套 `534/175/<linux digest>`，
这套数字只可能对 Linux 成立。

**规则**：

- 跨平台的产物扫描，后缀要按平台取（`.o` / `.obj`），不要写死。当前实现用
  `\.(?:o|obj)`，第二处读 `socket_pool_additional_capacity` 对象的代码也做了同名回退。
- 基线要按**平台族**给，不能只给一份。当前用 `BASELINES = {linux, win, macos}`，
  每项是「源文件数 / 读取点数 / sha256」，并由构建目录名（`MiniCronet-linux-*` 等）
  选择；名字认不出来时**直接报错**，绝不回退到某个默认基线——回退会让新增平台静默
  借用别的平台的数字，那等于没有门禁。
- 新增平台 = 量一次并加一行，不是改一个全局常量。

**门禁**（都要有，缺一不算修好）：

1. 8 个平台各自的 `audit-core-*.sh` 全部通过。
2. 变异测试：把某平台的 digest 或读数改错，该平台审计必须非零退出。
3. 变异测试：把后缀改回只有 `.o`，Windows 必须重新回到 `sources=0` 并失败——
   这证明后缀修复是**承重的**，不是装饰。
4. 变异测试：故意让 `audit-core-windows.sh` / `audit-core-macos.sh` 跑一次坏基线，
   必须失败——这证明扫描确实**被接进去了**，而不是又一次「调用了但没生效」。

第 4 条是这次真正该记住的：修完脚本本身还不够，**要证明每个平台的审计真的会执行它**。

---

## 45. 「Session 里存着一堆字段」不等于「传输上下文是一个值」

**现象**：`Session` 的传输相关配置（profile / 代理 / 代理凭据 / TLS 校验 / CA /
UA / Accept-Language / 协议钉 / HTTP cache）散在 `__init__` 的十几个属性里，由
`_engine_config()` 每次请求现场拼成一个 `EngineConfig`。三个后果各自都真实发生过：

1. **无法回答「这条请求会不会复用我刚建好的连接？」** 只能反推 `EngineConfig` 的
   键，而键的顺序和成员写在一个 `_FIELDS` 元组里，与构造函数是两份东西。
2. **`generation` 混在同一个键里**，于是「cookie store 换代」和「换传输上下文」在
   缓存键上无法区分——`discard_cookies` 的历史缺陷（§43）正是从这个混同里长出来的。
3. **镜像 cookie 无锁**：`_resolve_cookies` 是「读 jar → 读 mirror → 比对 → 可能换
   generation」四步，`_absorb_cookies` 是三个 jar 的更新。两个 worker 交错时可能各自
   拿到不同 generation，于是一个请求发出的 `Cookie` 头会被它自己引擎的 store 覆盖。

**根因**：传输身份是一个**值**，但它被写成了「对象上的一组可变属性 + 一个拼装函数」。
值可以被比较、被哈希、被传出去；属性不行。

**修法**：

- 新增 `SessionBinding`：不可变、可哈希，字段固定为 `BINDING_FIELDS`，
  `generation` **不在其中**。它同时是引擎缓存键（`EngineConfig` = binding +
  generation），也是 `Session.binding(url)` 的返回值——于是「共享连接池」从推断变成
  `==` 比较。
- `EngineSlot` 记录 in-flight 计数，`EngineCache.acquire()` 返回租约，cookie 解析换
  引擎时 `lease.rebind()` 把租约挪到新 slot。淘汰一个仍在使用的 slot 会被记成
  `deferred`，不再看起来是「免费的淘汰」。
- `_cookie_lock` 把四步序列和 mirror 更新各自变成原子操作；`EngineSlot` 再持一把
  自己的锁。
- `Session.stats` 把 `builds`/`hits`/`evictions`/`deferred`/`inflight` 暴露出来，
  §29 的「每个请求重建上下文」不再是只有 TIME_WAIT 才能发现的事。

**门禁**：`bindings/python/tests/test_session_binding.py`。变异测试确认过每条都
能被证伪：去掉租约 `_retain()`、去掉 `_cookie_lock`、把 `_take` 的切片偏移改错、
把 `deferred` 计数改成 `+= 0`，对应测试各自失败。其中锁的那条必须用 `time.sleep`
而不是忙等——忙等不放 GIL，坏实现也会看起来是串行的。

**代价记在账上**：`EngineConfig` 与 `SessionBinding` 有一份字段重叠，靠
`EngineConfig.binding` 与 `_binding()` 单向派生保证不漂移；
`test_any_transport_field_separates_the_key` 遍历 `BINDING_FIELDS`，新增字段不参与
缓存键会立刻失败。

## 46. 「不可得」与「没接上」是两件事

**现象**：README、`docs/COMPATIBILITY_BOUNDARY.md`、`docs/NEXT_STEPS.md` 与
`impersonate.py`/`utils.py` 的注释一致声称：`impersonate=` 只换 `User-Agent` 版本串，
Chrome 的 `sec-ch-ua*`/`Sec-Fetch-*`/`Accept` 由 `services/network` 的 URLLoader 工厂层
注入，`net::URLRequestContextBuilder` 没有入口，因此**不可得**，要等 ABI v9。

这个说法**对一半**：需要 ABI v9 的是"让 Core 自己注入"。facade 层完全可以注入，
而它一直没做。实测两件事：

1. 72 个 profile 的 h1 抓包全部逐字节一致，六条抓包之间零分歧——证据一直在仓库里；
2. 把这些头作为 extra headers 传进去，`ExtraFingerprints(header_order=...)` 能把顺序
   也复原，**72/72 逐字节且顺序一致，diffs=0**。

所以这是**接线缺口**，不是能力缺口。文档把"Core 层不可得"写成了"整体不可得"，
于是一个可修的缺陷被记成了已声明的边界——这比缺一个头危险得多，因为它让下一个人
不再去看。

**修法**：`tools/generate-client-hints.py` 从抓包生成 `client_hints.py`
（`CAPTURED_HEADERS`），`browser_headers.py` 把它变成有序头列表，
`BaseSession._browser_headers` 在唯一的 `prepare_request` 入口合并——同步与异步共用，
不可能分叉。两处刻意偏离抓包，都在文档里写明理由：

- **平台跟随宿主**：抓包来自 Windows，但 `User-Agent` 括号组与 `sec-ch-ua-platform`
  都按宿主重写，与 `core/source/minicronet.cc` 自己的推导一致。`"Windows"` 的 hint
  配 `X11; Linux` 的 UA 比缺一个 hint 更容易被检出。
- **`Sec-Fetch-*` 取导航语义**：每条抓包都是导航，`session.get(url)` 也是导航。

**顺带修掉的既有缺陷**：`headers={"X-Gone": None}` 发的是字面量 `X-Gone: None`。
`Headers.add` 把 `None` 交给 `_text` 字符串化了，而 requests 的语义是"删除"。
`Headers` 现在记录删除（`deleted_names`），`merge_setting` 把它转发到合并结果，
所以删除对注入的默认值也生效。

**门禁**：`tools/audit-client-hints.py`（AGENTS.md §5）从抓包重新推导整张表，并检查
UA 版本串与 Core 的 `kFullVersions` 一致、`sec-ch-ua-platform` 出现在抓包有的那些版本上。
变异测试四条全部非零退出：改一个值、交换两个头的顺序、删掉一个 profile、给 chrome_86
凭空加一个它从未发过的 hint。`test_client_hints.py` 17 条覆盖 72/72 逐字节、覆盖语义、
两条删除路径、`default_headers=False` 与 `impersonate=None` 都不注入；变异测试四条
（关掉注入、冻结平台、不重写 UA 组、丢掉删除记录）各自失败。

**规则**：文档写"不可得"之前，先分别验证"Core 层有没有入口"和"绑定层能不能做到"。
前者是 ABI 事实，后者是接线问题，两者混写会让可修的东西看起来修不了。

---

## 47. `is_minicronet_build` 与 `is_cronet_build` 是两个标志

**现象**：审计 `URLRequestContextBuilder` 的 41 个 `set_*` 入口时，我判定六项与本项目
无关，其中三项给出的理由是「需要服务端配合」「需要 OS 级安全硬件」「客户端库没有
上报端点」。这三个理由**都是推断，不是源码依据**。

**复核后的真实原因**是另一回事：

```gn
# net/features.gni:40
enable_reporting = !is_ios && !is_minicronet_build

# net/features.gni:63-64
enable_device_bound_sessions =
    !is_minicronet_build && (is_win || is_linux || is_mac)
```

```c
// net/url_request/url_request_context_builder.cc:581
#if !BUILDFLAG(MINICRONET_BUILD)
  if (enable_shared_dictionary_) { ... }
#endif
```

三项都是**被 `is_minicronet_build` 条件编译掉**，不是「不需要」。这个差别很大：前者
意味着即使想做也做不了（除非改 `MINICRONET_BUILD` 的语义），后者是设计取舍。

**根因**：我把「cronet 关掉的」当成了「minicronet 也关掉的」。它们**是两个独立标志**，
而 `sync-core.sh` 之所以有一串补丁，正是在手工把 `is_minicronet_build` 并进若干原本
只判断 `is_cronet_build` 的位置。反例就在同一份文件里：

```gn
# net/features.gni:48  —— 只判断 is_cronet_build，不判断 is_minicronet_build
include_transport_security_state_preload_list = !is_cronet_build
```

所以 **HSTS 预置列表在 Core 里是编进去的**，`TransportSecurityState` 对象也确实存在
——缺的只是「没接上」，而不是「没有」。如果我按「cronet 关了就等于 minicronet 关了」
推断，会把一个可修的能力误判成不可得，与第 46 条是同一个错误。

**规则**：判断某项能力在 Core 里是否存在，必须逐项去 `net/features.gni` 与对应
`BUILD.gn` 里看**用的是哪个标志**，不能看项名，也不能从 cronet 的行为外推。

**如何复查**：

```bash
grep -rn "<该项的开关名>" $CHROMIUM_SRC/net/features.gni \
    $CHROMIUM_SRC/net/url_request/url_request_context_builder.*
```

看结果里是 `is_minicronet_build`、`is_cronet_build`、`BUILDFLAG(MINICRONET_BUILD)` 还是
毫无条件——四种情形的结论完全不同。

**否定性结论同样需要证据**（AGENTS §11）：写「这项无意义」时，要能指出具体是哪一行
让它无意义。给不出行号，就说明还没查。

---
## 48. 「构建不可复现」是我的误判：缺的是 pkg-config，不是架构冲突

这一条最初写的是「`gn gen` 从来跑不通，`//ui` 的平台假设与 `is_minicronet_build`
不可调和」。**结论是错的**，而且错得很有代表性，所以保留整条记录。

### 当时的推理

往 `args.gn` 加 `use_thin_lto = true` 后重建，`ninja` 返回 `no work to do`。
强制 `gn gen` 撞上：

```text
ERROR at //ui/gtk/BUILD.gn:16  assert(use_gio, "GIO is required for building with GTK")
```

补 `use_gtk = false` 后撞下一环 `//ui/platform_window/common:8 assert(use_ozone)`，
试 `gn_all` 置空后又撞 `//ui/native_window_tracker:8`。逐步推出「四环断言链」，
并断定「继续修等于逐个 patch 掉 `//ui` 的平台假设，那是重写抽象而不是调配置」。

### 实际的错误只有两处

**一、手动跑 `gn gen` 时没带 `--root-target`。** ninja 触发重生成时用的是：

```text
gn --root=../.. -q --fail-on-unused-args --regeneration \
   --root-pattern="//minicronet:*" --root-target=//minicronet gen .
```

只解析 `//minicronet` 及其依赖。`//ui/gtk`、`//ui/gl`、`//ui/native_window_tracker`
都不在这棵子树里——它们是 `gn_all` 底下的测试目标，只有全量 gen 才会碰到。

**二、真正的报错在更前面，我停在了第一处。** 带 `--root-target` 跑，报的是：

```text
FileNotFoundError: [Errno 2] No such file or directory: 'pkg-config'
See //build/config/linux/nss/BUILD.gn:11:3: whence pkg_config was called.
```

本机没装 pkg-config。而 `docs/BUILD.md` **早就写了这个坑和解法**——解包
`pkgconf` / `pkgconf-bin` / `libpkgconf3` 到 `/tmp/pkgconf/root`，设好 `PATH` 与
`LD_LIBRARY_PATH`。我没读到那一节就下了结论。

### 装好 pkgconf 之后

九个平台的 `gn gen` 全部静默成功。0.2.10 就是用它从零重建的：

| 平台 | 产物 | 大小 |
| --- | --- | --- |
| linux-x86 | `libminicronet.so` | 8,063,628 |
| linux-x86_64 | `libminicronet.so` | 8,368,280 |
| linux-armv7 | `libminicronet.so` | 6,082,252 |
| linux-arm64 | `libminicronet.so` | 8,013,312 |
| macos-x86_64 | `libminicronet.dylib` | 7,958,968 |
| macos-arm64 | `libminicronet.dylib` | 7,310,880 |
| windows-x86 | `minicronet.dll` | 7,501,312 |
| windows-x86_64 | `minicronet.dll` | 9,416,192 |
| windows-arm64 | `minicronet.dll` | 8,118,784 |

九个都带 `minicronet/0.2.10` 版本串。Windows 三个另需 ripgrep（脚本的
Android/Java/JNI 泄漏检查用）与 `xwin-winsysroot`。

### 这条教训真正的内容

**一个「不可调和」的结论，往往只是还没找到正确的那条命令。** 当时的证据链看起来
很扎实：四环断言逐一实测、两套补救方案都试过、还从源码里找到了 `use_gtk =
is_linux && !is_castos` 这样的根因。但整条链建立在一个错误的前提上——我跑的不是
构建系统真正跑的那条命令。

两次都是同一个错误模式：**停在第一个报错上，把「我没走到那一步」当成「那一步走不通」**。
第 47 条（`is_minicronet_build` 与 `is_cronet_build` 是两个标志）是这个模式，
第 46 条（「不可得」与「没接上」是两件事）也是。三条连起来看，这是我最稳定的失误来源。

**可操作的规则**：

1. 判断某个工具「跑不通」之前，先从构建系统里取出它**实际执行的完整命令行**
   （`ninja -t commands`、`-t targets`、`build.ninja` 里的 rule），而不是凭印象手敲。
2. 报错只读第一条就下结论是危险的。继续往下滚，后面的错误可能推翻前面的推断。
3. 项目文档里「已知问题」那一节要读——这次的答案就在 `docs/BUILD.md` 里，
   写的时候我还读过那一节。

### 关于「产物可复现」

`manifest.json` 的 sha256 现在可以当作**可复现性声明**了：别人按 `docs/BUILD.md`
的流程、用同样的 pkgconf / ripgrep / xwin，能从这份 commit 重建出同样的产物。

`tools/record-core-build-args.py` 仍然有价值，但理由变了：它不再是为了「记录一份
无法重建的历史配置」，而是保证「重建时改了配置会被发现」。`--check` 已接入 AGENTS §5。

## 新增 profile 的三级证据链：两个真实缺陷

**现象 A**：`tools/audit-profile-addition.py` 判定 chrome_157 的源码不存在——
`ssl/t1_lib.cc does not exist at boringssl 5b83e5d1abf0`。而同一份表里
`tools/collect-profile-evidence.py` 解析同一个文件却完全正确。

**根因 A**：`PATH_ERAS` 是「按版本从旧到新排列」的表，取某个 major 的路径要取
**最后一个**适用的 era。采集器用 `path = mapping[name]` 在正向循环里赋值，最后
一次赋值胜出，恰好正确；新门禁写成 `return mapping[name]`，**第一次匹配就返回**，
于是 157 拿到的是 86 的路径。同一张表，两种写法，一个对一个错。

**规则 A**：同一张历史映射表只能有一份定义。新门禁现在
`from collect-profile-evidence import PATH_ERAS`，两处不可能再分叉。反向遍历 +
`return` 是唯一写法。

**现象 B**：变异测试把 chrome_86 的 h2 抓包复制到 `profiles/chrome_157/h2/`，
第 2 级**通过了**。文件有 6 条连接、稳定字段一致、随机字段各异——一切看起来正常。

**根因 B**：原来的检查只验证「这个文件内部自洽」，从不验证「这个文件属于这个版本」。
一份格式完好、数量正确、字段稳定的抓包可以是**别的版本**的数据，而所有下游对比
都会拿错参考。这不是理论问题：我自己在做第 3 级变异测试时就把 86 的数据留在了
157 的目录里，之后的 Level 2 全量扫描显示 72/72 通过。

**规则 B**：抓包必须自证归属。第 2 级现在读每条连接的 `Chrome/<version>` token，
major 不等于目录名即失败（`this capture is from Chrome 86 but it is filed under
chrome_157`）。同一套数据还加了哈希比对：`profiles/chrome_<major>/<proto>/captures.json`
必须与参考集里该版本该协议的样本逐字节一致。

**这两条共同的教训**：门禁的判据要指向**外部事实**，不能只指向对象自身的自洽性。
「六条连接、字段稳定」描述的是文件的形状；「这是 chrome_157 的数据」描述的是它和
世界的关系。前者可以被一份错文件满足，后者不能。

**现在由什么守住**：`tools/audit-profile-addition.py --levels 2` 的归属断言，
变异测试（把 86 的数据放进 157 目录）确认它会非零退出。

---

## chrome_152 的 trust_anchors：28 是被污染的观测，不是版本行为

**现象**：Core 发出的 chrome_152 ClientHello 带 28 个 trust anchor ID，真 Chrome 152
带 32 个。文档把这件事写成「编译期根库 32 个、抓包 28 个是子集」，读起来像「28 才是
对的、32 是根库的完整状态」——方向反了。

**根因**：早先那次抓包只采到 28 个，据此推断「抓包机的根库被 PKI Metadata 组件更新过，
任何源码树里都没有这个状态」。这个推断本身没错，但那台机器的根库被动过，意味着 **28 是
被污染的观测**，不能当 Chrome 152 的行为冻结进 profile 表。当时的 README 甚至点名了缺的
4 个（`d6790902`、`d6790903`、`d6790909`、`d679090e`，都是 11129.9.x），却在下一句把它们
当成「按观测冻结」的正当理由。

**新证据**：新的参考抓包集里 chrome_152 的六条连接**全部是 32 个**（`common=32
union=32`），且严格包含旧的 28 个，多出的正好是那 4 个。153–157 仍是 28 个，与表逐字节
一致。

**规则**：抓包里出现的「本机状态」不等于「版本行为」。当观测值与编译期常量不一致时，
默认假设是**观测被污染**，而不是「Chrome 用了子集」。要冻结一个子集，必须能指出上游代码
里**为什么会少**——这里指不出来，因为 `SelectAllTrustAnchorIDs()` 无条件发全部。

**修复**：`profiles/normalized_profiles.json` 的 152 `trust_anchor_ids_encoded` 从 184
字节 / 28 个改成 204 字节 / 32 个；重新生成表；8 个平台同批重建（linux-arm64 +64 字节，
其余不变，因为数组共用对齐）。实测 Core 现在发 32 个，与真 Chrome 一致。

**现在由什么守住**：`tools/audit-profile-addition.py --levels 2` 按参考抓包核对 trust
anchor 的 **ID 集合**（不只是数量）。变异测试：把 152 改回 28 个，门禁报 `the table
stores 28 trust_anchor IDs but the captures advertise 32; missing from table:
[d6790902, d6790903, d6790909, d679090e]`。

h3 抓包只记录扩展名与 id、不含载荷，所以 h3 一侧记为 `match: null`（未核对），不记为
通过——数据能力不足不能冒充证据。

---

## 跨平台一致性：把「跑不了」变成「可判」

**现象**：8 个平台的 Core 都重建了，但只有 linux-x86_64 能执行。问「另外 7 个平台的行为
和 linux-x86_64 对齐了吗」时，原始答案是「不知道」——没有 qemu、没有 wine、没有 32 位运行时。

**根因**：`audit-core-binaries.sh` 检查的是 manifest 自洽性（SHA-256、大小、架构、导出符号）。
这些全部通过**并不能**说明产物里装的是当前那张表的数据：一个用旧表编译的产物，它的
manifest 也是完全自洽的。这正是 chrome_152 的 28 vs 32 能活过一个发布周期的原因。

**规则**：当一件事无法通过运行验证时，要找到**能静态判定**的那部分并把它变成门禁，同时
如实声明剩下那部分是空白。不能因为「跑不了」就整条放弃，也不能因为「静态一致」就写成
「已验证」。

**做法**：`tools/audit-core-payloads.py` 从生成头里取出 `trust_anchor_ids_*` 数组（定义），
再要求每个产物都逐字节嵌入它。trust anchor 是最锋利的见证：它是 `uint8_t[]`，与端序、
指针宽度无关；由 `SelectAllTrustAnchorIDs()` 原样发出；而且它是唯一一个「值错 = 指纹错」
而不是「值错 = 性能差」的字段。

**能证明的**（强）：8 个产物嵌入的载荷逐字节相同；载荷与 profile 输入一致；相关代码
路径没有平台分支；`SelectAllTrustAnchorIDs()` 不读系统根库。

**不能证明的**（无证据）：7 个平台能加载、能发请求、发出来是 32 个。按 AGENTS §12 记为
「已构建、载荷静态一致、未运行」。

**变异测试**：换入旧表编译的 linux-x86 产物 → `does not embed trust_anchor_ids_0
(204 bytes)`；把生成头改一个字节 → `the generated table is stale`。

**教训**：静态一致和运行验证是两个不同的命题，中间没有捷径。把前者做成门禁能挡住
「陈旧产物」这一类真实缺陷，但它不能替代后者——文档里必须把两者分开写。

---

## 49. 共享状态被当成本地状态：两处 P0 是同一个形状

**现象**：两个各自独立、都通过单线程测试的机制，在并发下把状态改到了别人的请求上。

| 机制 | 单线程表现 | 并发表现 |
| --- | --- | --- |
| `cert=` 每请求重新注册 | 正确，只是慢 | 正确，但每个请求都刷新该 origin 的连接组，连接池与 H2 复用被摧毁 |
| `discard_cookies=True` | 正确 | 清掉**别的**请求的 cookie，并置会话级 mute 把别人的写入也吞掉 |

**根因**：两者的状态本该属于「这一次请求」或「这个引擎」，却被存放在「这个会话」上。
`cert=` 的「已注册」没有记录，于是每次调用都得当作首次；`discard` 的「不发送不记录」
没有每请求表达，于是用「先把世界抹掉」当作等价物。

Chromium 侧的原因各有一句源码可依：

- `SSLClientContext::SetClientCertificate` 同步调 `NotifySSLConfigForServersChanged`
  （`net/socket/ssl_client_socket.cc:274-287`），后者让 `HttpStreamPool` 刷新该 origin
  的连接组。函数本身没错，错的是把它放在每请求热路径上。
- `CookieMonster` 是 per-context 单例，`Cookie` 头由 `URLRequestHttpJob` 自己附加
  （`net/url_request/url_request_http_job.cc:761-838`），请求层面没有「这次别带」的开关
  ——除非走 `allow_credentials` / `LOAD_DO_NOT_SAVE_COOKIES`，而 ABI v11 没有把它们
  暴露出来。

**规则**：

- 一个判断「要不要做」的成本，必须和「做了之后影响多大」一起看。重注册证书的代价不是
  一次 PEM 解析，是**该 origin 的所有空闲连接被关掉**。
- 用「清空共享状态」实现「这次不用状态」时，必须先回答：还有谁在读它。答不上来就别写。

**门禁**：

- `EngineSlot._applied_certs`：同一 (host, port) 的相同字节只注册一次，记录按 **Core 调用
  完成的顺序**提交（最后返回的那次就是 Chromium 保留的那次）；`_pending_certs` 让同一个
  值的并发调用合并成一次 Core 调用，`clear_client_certificate` 等待在飞的注册。
  回归：`test_client_certificates.RegistrationMemoTests`（7 条）。**这 7 条是必须的**：
  把记忆化短路掉之后，原来的 7 条功能测试全绿——它们断言的是"服务器看到了哪张证书"，
  而那件事在不记忆化时也成立。**变异测试**：`if self._applied_certs.get(key) == value:`
  改成 `if False:` → `test_the_same_bytes_reach_the_core_once` 立刻红（3 != 1）。
- `discard_cookies` 走 `_throwaway_slot`：该请求的引擎不可从缓存到达，store 无人共享。
  回归：`test_session_defects.DiscardCookiesIsolationTests`，新增一条并发用例。
  **变异测试**：把实现改回 `slot.clear_cookies()` → 新用例立刻失败并打印 `[sid=BBB]`；
  补丁下 20 次读 0 次错，补丁前 19/20 错（独立探针）。

**顺序那一层不是过度设计，但方向必须是"完成顺序"**：两个线程为同一个 server 注册
**不同**证书时，Core 调用的完成顺序可能与取锁顺序相反。第一版实现用"调用前取号、号大者写"，
在那种交错下会把记录写在**先完成的那张**上——而 Core 实际保留的是后完成的那张。
实测（假引擎复现同一交错）：`core_holds=CERT_X`、`record_says=CERT_Y`、
`record_matches_core=false`，且下一次**相同**的 CERT_Y 注册被跳过（Core 调用 0 次），
服务器继续看到 CERT_X。改成"谁完成谁落地"之后两者一致。

**没有被测到的东西写进代码就是未经验证**：上面这段交错在本轮之前没有任何测试覆盖，
因为它要求 Core 调用乱序完成，功能测试构造不出来。`RegistrationMemoTests` 用假引擎直接
指定完成顺序，这是这类缺陷唯一能稳定复现的形状。

**代价（写在明处）**：`discard_cookies=True` 每个请求多一次上下文构建并自带一条连接，
因此**不复用 TLS 会话、不共享 HTTP 缓存**。实测分三段：`EngineConfig.build()` 2.36–4.04 ms、
拆除 2.16–3.51 ms、以及一次全握手（`session_reused=False`，ClientHello 无 `pre_shared_key`）。
构建已挪到 `run_in_executor`、拆除已挪到 `retire_engine_slot` 的后台线程，所以这些耗时不再
落在事件循环上：16 并发 discard 的 loop 停摆从 83 ms 降到 9.5 ms（同负载无 discard 为 2.8 ms）。
这是 fail-closed 的正当成本，也是指纹面上必须具名的代价（`tools/audit-discard-fingerprint.py`）。

**这条代价短期消不掉，原因是查证过的**（见 `docs/TBD_VERIFICATION.md` T-1）：

- `net::LOAD_DO_NOT_SEND_COOKIES` **在 M87 就被删了**（M86 还有，`1 << 8`）。
  仓库 Core 是 M156，所以「给 ABI 加一个抑制字段」没有对应的 Chromium 机制可用。
- M156 里唯一的 per-request 入口是 `URLRequest::set_disallow_credentials()`，但它
  同时置 `LOAD_DO_NOT_SAVE_COOKIES`，并在 `DeterminePrivacyMode()` 里把请求降级成
  `PRIVACY_MODE_ENABLED(_WITHOUT_CLIENT_CERTS)`——**会连带改掉客户端证书行为**。
  用它实现 `discard_cookies`，等于让「不带 cookie」顺带变成「不带客户端证书」。
- 所以「换一个 store」不是权宜之计，而是当前唯一只影响 cookie 的开关：`CookieMonster`
  是 per-context 单例，没有 per-request 的删改入口。

**教训**：不要写「等 ABI 加上就能消掉」这种话——它把一个尚未查证的假设写成了路线图。
本轮就是照这句话去查 T-1 的，结果三条前提全不成立。

---

## 50. 一个 `except` 只认一种异常，就把「无事发生」写成了「永远等待」

**现象**：mTLS 会话里按下一次 Ctrl-C（或任何让调用方在 Core 等待中退出的异常）之后，
该引擎对**那个服务器**永远无法再注册证书：下一次 `set_client_certificate` 与
`clear_client_certificate` 都永久阻塞。实测：`_pending_certs` 停在
`{("example.test", 443): (b"CERT", b"KEY")}`，随后两个调用都等不满 2 秒的 join。

**根因**：`EngineSlot.set_client_certificate` 用「先占位、再调 Core、再落地」的方式
让并发注册合并成一次 Core 调用，占位只由 `except RuntimeError` 释放。
`KeyboardInterrupt` / `asyncio.CancelledError` 是 `BaseException`，不走那个分支，
占位因此留在表里；而 `wait()` 等的是条件变量，没有第二个人会 `notify`。

**规则**：任何「我先声明占用，后面一定释放」的状态，释放必须写在**所有**退出路径上，
包括 `BaseException`。用 `except Exception` 去覆盖「所有失败」永远是错的——它覆盖的是
「所有我认为会发生的失败」。

**门禁**：`test_client_certificates.RegistrationMemoTests` 新增两条——被中断的注册不得
留下占位，且之后的 `set` 与 `clear` 都必须返回。**变异测试**：把 `BaseException` 分支里的
`_abandon_pending_cert` 换成 `pass` → 两条立刻红（`{("example.test", 443): ...} != {}`）。

---

## 51. 「替换」被写成了「删除」，于是一个默认值悄悄盖掉了调用方

**现象**：调用方用最自然的方式构造请求头（先给默认值，再改其中一项），发出去的请求
**丢掉了**会话级同名字段。实测：`Headers({"Accept": "one"}).update(Headers({"Accept": "two"}))`
之后 `deleted_names == {"accept"}`，`merge_setting` 于是把会话的 `Accept` 整条删掉。

**根因**：`Headers.update` 为了替换同名字段调了 `del self[name]`，而 `__delitem__` 的定义
就是「记录一次删除」——它同时被 `{"Name": None}` 的删除语义使用。一个内部实现动作
复用了另一个公开语义。

**规则**：当一个类型的某个操作**有语义**（删除会被合并层读取）时，内部实现不得调用它来
达到别的目的。替换与删除要在实现里分开写，哪怕它们最终都改动同一条记录。

**门禁**：`test_session_defects.HeaderDeletionRecordTests`（6 条）覆盖两个方向——替换不留下
删除记录、真正的删除仍然穿过 `merge_setting` 生效。**变异测试**：删掉 `__setitem__` 里清理
记录的那一行 → `test_assignment_clears_an_earlier_deletion` 立刻红。

**同一形状的第二处**：`Headers(Headers({"Sec-Fetch-User": None}))` 丢掉了删除记录，于是
profile 的默认值又被发了出去——删除只存在于记录里，而复制只复制了 items。

---

## 52. 代理绕过名单错的方向只有一个：**该绕的没绕**

**现象**：`NO_PROXY=*` 与 `NO_PROXY=.example.com` 两种写法都不生效，请求走了调用方
明确排除掉的代理。实测：`HTTP_PROXY` 指向死端口 + `NO_PROXY=*`，
`_effective_proxy` 仍返回该代理。

**根因**：`should_bypass_proxies` 只做了「后缀匹配 + CIDR」两件事。`*` 不是任何主机的后缀，
而前导点是 cookie 文件的写法，比较时没被剥掉。两者都不报错，只是静默地不匹配。

**规则**：一份**排除**名单，失效的方向必然是「该排除的没排除」。凡是依赖名单默认行为的
开关（`*`、前导点、空字符串）都必须有单独的用例，且用例要断言**排除生效**，不是断言
「函数返回了布尔值」。

**门禁**：`test_session_defects.NoProxyWildcardTests`（4 条，含一条走完整环境变量的路径）。
**变异测试**：删掉 `*` 分支 → 两条立刻红。

---

## 53. 同一个响应，`.text` 与流式解码给出了两个不同的答案

**现象**：一个 `Content-Type: text/plain`（无 charset）的 Latin-1 响应，`.text` 正确，
`iter_content(decode_unicode=True)` 把重音字符变成 U+FFFD。

**根因**：`get_encoding_from_headers` 对没有 charset 的 `text/*` 返回 `None`。`.text` 于是
落到 `apparent_encoding`（探测，猜中 Latin-1），流式路径落到 `default_encoding`（utf-8）。
两条路径各自「合理」，合起来是矛盾。RFC 2616 §3.7.1 对这一格有明确默认值，requests 也用它。

**规则**：**规范没有留白的地方不要返回 `None`**。返回 `None` 等于把这件事交给下游各自猜，
而下游不止一个。

**门禁**：`test_session_defects.TextEncodingAgreementTests`——同一条响应必须两条路径解出
同一个字符串，且不含替换字符。**变异测试**：删掉 `text/*` 分支 → 两条立刻红。

---

## 54. 发送一个请求，不等于可以改写调用方的那个请求对象

**现象**：`session.send(prepared, discard_cookies=True)` 之后再 `session.send(prepared)`，
第二次也没有 Cookie 头。实测：调用方对象上的 `Cookie` 头在第一次发送后就是 `None`。

**根因**：`discard_cookies` 的实现在 `prepared.headers` 上直接 `pop("Cookie")`。那个对象是
调用方构造并持有的，requests 的契约是 `send()` 不改写它。

**规则**：**入参是借来的**。一个函数为了自己这次的语义需要「同一份数据但少一项」时，
复制出它需要的那份，不要就地修改。

**门禁**：`test_session_defects.DiscardDoesNotRewriteTheCallersRequestTests`（3 条，含异步路径）。
**变异测试**：把复制去掉、改回就地 `pop` → 两条立刻红。

---

## 55. 「构造时规范化」不等于「入口规范化」

**现象**：`Session(http_version="h2")` 一直可用，而 `get(url, http_version="h2")` 报
`unknown http version`。同一个值在两个入口一个接受一个拒绝。此外 `"v2"`、`"h2"`、
`"HTTP/2"` 在引擎缓存里是三个不同的键，一个会话为同一条线路身份建了三个上下文。

**根因**：`__init__` 里调了 `normalize_http_version`，而 `_binding()` 里直接写
`http_version if http_version is not None else self.http_version`——把每请求值原样透传。
旁边 `impersonate` 就先规范化再传，因为它曾经踩过同一个坑（`"CHROME_155"`）。
**同一个文件里已经有一条修好的先例，新字段却没有照抄。**

**规则**：一个字段的取值只要有规范形式，规范化就必须发生在**所有**通往 Core 的入口，
而不是构造函数的末尾。缓存键由这个字段组成时，未规范化的值还会**复制**出多余的
上下文——缺陷从「一次报错」变成「持续多花一个引擎」。

**门禁**：`test_session_defects.PerRequestHttpVersionTests`（4 条：同义写法归一、
缓存键相同、非法值仍然 fail-closed、缺省沿用会话值）。**变异测试**：把每请求的
`normalize_http_version` 去掉 → 3 条立刻红。

---

## 56. 同级参数各有各的校验规则，等于没有规则

**现象**：`max_engines=None` 从引擎缓存内部抛出一个裸 `TypeError`；`max_engines="8"`
被静默接受；`max_engines=0` 与 `-1` 被静默改成 1。而 `max_response_bytes`、
`max_redirects`、`max_sockets_per_group` 对同样的输入都抛 `ValueError`/
`UnsupportedFeature` 并说明原因。

**根因**：`EngineCache.__init__` 里一行 `max(max(1, int(max_engines))` 就完成了「校验」，
而它旁边的三个兄弟各有一个具名校验函数。写新参数时**没有先看同一族参数怎么校验**。

**规则**：同一个对象的同级参数必须共用一套校验风格——类型错、越界、缺省，三者要么都
报错要么都不报，且异常类型一致。读取时才发现不一致，说明缺的不是一个校验函数，
而是一条「新参数照抄兄弟参数」的纪律。

**门禁**：`test_session_defects.EngineBudgetValidationTests`（4 条）。**变异测试**：
换回 `max(1, int(max_engines))` → 1 条 failure + 1 条 error 立刻红。

---

## 57. 同一个失败有两条到达路径，就只有一个路径上的异常类是对的

**现象**：会话 `close()` 之后，`session.get()` 抛 `SessionClosed`，而
`session.cookies.set()` 与 `session.cookies.clear()` 抛的是基类 `RequestException`。
调用方写 `except SessionClosed` 包住 cookie 操作时**漏掉了它**。

**根因**：关闭状态有两个入口。`BaseSession._ensure_open` 显式抛 `SessionClosed`；
而 cookie jar 走 `_store_slot()` → `EngineCache.get()`，那里写的是
`raise RequestException("Session is closed")`。两条路径的字符串一模一样，类不一样——
**字符串相同掩盖了类型不同**，所以读代码时看不出来，只有实际 catch 才发现。

**规则**：一个状态如果有多条查询路径，它抛出的异常类必须由**一个**定义决定。
判据不是「消息写对了」，而是「`except <最具体的类>` 在每条路径上都成立」。
消息是给人看的，类型是给程序看的，前者一致不构成任何保证。

**门禁**：`test_session_defects.ClosedSessionExceptionTests`（4 条：请求、cookie 写、
cookie 清、引擎查找）。**变异测试**：把缓存改回抛基类 → 3 条立刻 error。

---

## 58. 一个选项的失败被归到另一个选项头上，比没有错误信息更糟

**现象**：`verify="/missing.pem"` 抛出裸 `FileNotFoundError`；把内容换成非 PEM 文本后，
同一个选项抛 `InvalidURL`，消息是 `InvalidArgument`——**告诉调用方 URL 格式有误**。
前者还不是 `RequestException`，写 `except RequestException` 包住请求的代码完全漏掉它。

**根因**：两条不同的失败都被压成了一个没有上下文的令牌。读取失败发生在 `open()` 里，
类型是文件系统的；解析失败发生在 Core 里，Core 对「CA 解析不了」和「URL 不合法」
回的是同一个 `InvalidArgument`，而 `map_native_error` 只能按令牌分类。

**规则**：**在上下文还在的那一层做归类**。`EngineConfig.build()` 知道 `ca_pem` 是否被设置，
所以那里能判定这是 SSL 问题；下游拿到 `InvalidArgument` 时已经不可能知道。同样，
绑定层不能靠解析 PEM 来提前判断——那是 Core 的职责（AGENTS §1.1），能做的只是把
上下文映射对。

**门禁**：`test_session_defects.CaBundleErrorTests`（4 条）。其中 `test_neither_case_...`
直接断言「不得报成 URL 问题」，而不是只断言新异常类型——只断言新类型的话，改回
`InvalidURL` 会红，但改出第三个错误类型不会。**变异测试**：两处 CA 读取各自改回裸
`open()` 各红一次，`build()` 的归类去掉红两次——三个变异点分别对应三条独立的路径。

---

## 59. `bool(None)` 是 `False`，于是「没传」被读成了「关掉」

**现象**：五个参数都写着默认 `True`、文档只列 `True`/`False`（或再加一个路径），
但显式传 `None` 会静默选中**关闭**状态：

| 参数 | `None` 的实际效果 |
| --- | --- |
| `verify` | 关掉证书校验 |
| `cache` | 关掉 HTTP 缓存 |
| `default_headers` | 丢掉 profile 的全部 12 个请求头 |
| `trust_env` | 忽略环境代理 |
| `allow_redirects` | 不跟随重定向 |

**根因**：赋值写成了 `self.x = x` 或 `bool(x)`，而消费侧一律 `if self.x:`。
`None` 于是走完了「真值判断 → False → 关」这条链，全程没有任何一步报错。
这四个字里没有一处是错的，**错的是它们串起来的语义**。

**规则**：一个布尔开关如果文档只说 `True`/`False`，那 `None` 就不该是一个含义
未被定义的值。要么在入口把它读成「保持默认」，要么显式拒绝。**静默取 False 是最坏
的第三种**：`verify` 那一行让「配置里手滑写了个 None」变成「不再校验证书」。

**门禁**：`test_session_defects.NoneMeansDefaultTests`（5 条，四个 `None` + 一条
「显式 `False` 仍然是关」）。最后那条是必须的：只测 `None` 的话，把守卫写成
`True if x is None else True` 也能通过。**变异测试**：把五处守卫一起去掉 → 4 条红，
而「显式 False」那条保持绿，说明它测的是另一件事。

---

## 60. 「下层会拒绝」不是理由：拒绝的时机和归因同样是契约的一部分

**现象**：两个 `proxy_chain=` 的问题，都因为「反正 Core 会拒绝」而没在绑定层检查：

| 输入 | 修复前 | 修复后 |
| --- | --- | --- |
| `proxy=` + `proxy_chain=` | 首次请求时 `InvalidURL` / `InvalidArgument` | 构造时 `InvalidProxyURL`，指出两个选项 |
| `proxy_chain="not a proxy"` | 首次请求时 `InitializationFailed` | 构造时 `UnsupportedFeature`，指出是哪一条 |

**根因**：Core 确实会拒绝第一种（`minicronet.cc:569`），但拒绝发生在**第一次请求**、
并且借用了 `InvalidArgument` 这个「URL 不合法」的令牌——调用方看到的是「你的 URL 有问题」，
而实际是「你配了两个互斥选项」。第二种更糟：Core 在 `InitializeOnNetworkThread` 里遇到
解析不了的条目就静默 `return`，`context_` 保持空，`Start()` 返 false，绑定层只能报
`InitializationFailed`，**没有任何一处提到 chain**。

**规则**：一个错误在**哪个时刻**报、归到**哪个选项**头上，都是对外契约。
「下层会拒绝」只保证不会静默接受，不保证调用方能看懂。凡是在本层就能判定的冲突，
就在本层判——**判据是「我这层是否已经知道答案」，不是「下层会不会兜住」**。

**门禁**：`test_session_defects.ProxyChainValidationTests`（6 条）。其中
`test_the_refusal_does_not_wait_for_a_request` 单独断言「不得报成 URL 问题」，
`test_every_accepted_scheme_still_works` 断言六种合法形状（含 IPv6）原样通过——
少了后者，一个「全部拒绝」的实现也能让前几条变绿。**变异测试**：去掉两处守卫 →
3 条红。

---

## 61. 门禁用 `re.search` 时，**同一个文件里的第二处违规是不可见的**

**现象**：在 `sessions.py` 里再声明一次 `_CORE_OWNED_HEADERS = frozenset(...)`，
文件里于是有两处（第 222、1148 行），而 `tools/audit-layer-ownership.py` 仍打印
`passed`、退出码 0。

**根因**：门禁的三层叠加：

1. `re.search` 只回答「有没有」，不回答「有几处」；
2. `found` 字典的键是 `(path, pattern_index)`，同一文件的同一模式必然折叠成一条；
3. 那条键本来就在 `KNOWN` 里，于是「没有新增违规」，门禁通过。

三条各自都合理，合起来的效果是：**AGENTS §1.1 为同一条规则新增第二个检查点时，
门禁不会响**。这正是 §6 那句「改门禁前先问还有哪些同类载体没被扫到」要防的情况，
只不过这次漏掉的是**同一个文件里的第二处**。

**规则**：用模式匹配做门禁时，先分清这条模式是「调用点」还是「声明点」。
调用点本来就该出现多次；声明点在一个文件里出现两次就是重复实现，必须计数。
判据不是「消息对不对」，而是「把违规复制一份，门禁还响不响」。

**修法与门禁**：`^NAME =` 这类声明模式改用 `re.findall` 计数，`> 1` 即失败，并在
`--json` 里多报一个 `duplicate_definitions`；调用点模式仍然只判断存在。
同时在 `tools/mutation-fingerprint-gates.sh` 里加了第 6 个变异用例，**并验证了该用例
本身的翻转**：把守卫改成 `if False and ...` 之后，套件输出 `MUTATION NOT DETECTED`。

**顺带记两条被证伪的假设**（都实测过，写在这里免得下一个人重走）：

- 「凡模式匹配到多次就失败」**不成立**：`_negotiated_http_version` 在同一个文件里
  合法出现 2 次、`reason_from_status_line` 出现 3 次。按「多次即失败」改，门禁立刻
  对 5 处合法调用报红——我改成那样之后马上就回退了。
- 「用 `_CORE_OWNED_HEADERS_BACKUP` 这种改名能绕过」是**我的变异脚本自己避开了模式**，
  不是门禁的问题；换成精确名字后问题才暴露。变异测试写错会得到一个假结论。

---

## 62. `insecure_dns_mode` 不设，等于 `dns_nameservers` 被接受后忽略

**现象**：`dns_nameservers="127.0.0.1:1"`（保证没人监听）配上去之后，请求仍在 0.3–0.5 秒内
返回 200，且本地起的 UDP 监听**收不到任何包**。README 把这一行标成 ✓。

**根因**（Chromium 侧，逐层追到）：

1. `HostResolverManager::PushDnsTasks` 的第一行是
   `insecure_tasks_allowed = (insecure_dns_mode != kDisabled)`
   （`net/dns/host_resolver_manager.cc:1355`），禁用时不 push `TaskType::DNS`；
2. 只剩 `TaskType::SYSTEM` 兜底，所以请求照样成功、延迟正常，**看不出任何异常**；
3. `ManagerOptions::insecure_dns_mode` 的默认值就是 `kDisabled`
   （`net/dns/host_resolver.h:354`），而 Core 建 `ManagerOptions` 时从不设它。

**为什么 DoH 没受影响**：DoH 走 `SecureDnsMode`，`CanUseSecureDnsTransactions()` 只看
`doh_config.servers()` 是否为空，与 `insecure_dns_mode` 无关。所以同一个表格里 DoH 的 ✓
是真的，plain DNS 的 ✓ 是假的——**一行里混着两种状态，最容易被整体当成可信**。

**修法**：`resolver_options.insecure_dns_mode = InsecureDnsMode::kEnabledBuiltIn`，
并显式 include `net/dns/public/insecure_dns_mode.h`。

**验证（必须两端都测，只测一端会得到相反结论）**：

- 修复前：配置的 nameserver 上 **0 个 UDP 包**；
- 修复后：**4 个 UDP 包**，临时探针打印 `insecure=1 nameservers=1`；
- 失败回退仍正常（`127.0.0.1:1` 最终 200，但耗时从 0.3 s 升到 1.6 s，说明先试了配置的
  服务器再退到 SYSTEM，这是 `kEnabledBuiltIn` 的上游语义，不是回归）。

**这一条还有一个流程教训**：我先直接改了 `$CHROMIUM_SRC/minicronet/engine.cc`，
重建后行为毫无变化，因为**仓库里的 `core/source/engine.cc` 才是源头**，构建用的是
`tools/sync-core.sh` 装过去的副本。同一个文件在两处存在、只有一处参与编译时，
「改了没生效」的默认解释应该是**改错了副本**，而不是「我的分析错了」。

**产物状态（如实登记）**：源码修复已落在 `core/source/engine.cc`；`core/binaries/` 下
**八个平台产物未重建**——本机只能编 linux-x86_64，替换单个目标会让跨平台产物不一致，
而那正是本项目 profile 证据链禁止的状态。因此这条修复在**下一次全平台重建**时才生效于
发布物，在那之前 `core/binaries/` 里的 `.so` 仍表现为「忽略 dns_nameservers」。

---

## 63. 没有任何门禁比较 `core/source/` 与 `core/binaries/`

**现象**：`core/source/engine.cc` 上一轮加了 DNS 修复（`+11`），而 `core/binaries/`
里九个产物全部构建于**改动前一天**。全部门禁绿灯：版本串一致、体积上限一致、
payload 一致、revision 一致——因为 manifest 记录的正是这些，唯独**没有记录
「这份产物由哪份源码构建」**。于是「改了源码忘了重建」在仓库里完全不可见。

**根因**：manifest 的字段是为别的目的设计的（防篡改、防 ABI 漂移、防 profile
丢失），artifact 哈希只证明「文件没被改」，不证明「它与源码同步」。
两者是不同的问题，却看起来都在回答「产物对不对」。

**修法**：

- `tools/install-core-binaries.py` 在写 manifest 时记录 `core_sources_sha256`
  （`core/source/` 下 22 个输入的 SHA-256，跳过 `minicronet_version_generated.h`，
  因为 `sync-core.sh` 每次构建都会重写它）；
- `tools/audit-core-source-fingerprint.py` 重算并与九个 manifest 比较；
- 两个文件各自实现了一份哈希，所以**实测核对过它们给出同一个值**
  （`cf394cf2…`，22 files）——同一规则的第二份实现如果不核对，本身就是新的漂移源。

**门禁**：`tools/mutation-fingerprint-gates.sh` 第 7 例。变异测试覆盖六个方向：
未记录 → 1、记录后 → 0、改一个源码 → 1、还原 → 0、删掉一个 manifest 的键 → 1、
还原 manifest → 1。**六个方向都跑过**，不是只测「坏的时候会红」。

**当前状态（如实登记）**：这个门禁现在**是红的**，因为九个产物确实早于源码改动。
它红得正确——它说的就是事实。要让它变绿只有一条路：一次全平台重建 + 重新记录指纹。
在那之前，`AGENTS.md` §5 的「提交前必须全绿」对这一条无法满足，这是**已知且被登记的
状态**，不是被绕过的门禁。

**这一轮我自己犯的两个错，一并记下**：

- 变异测试里用 `git checkout core/source/engine.cc` 还原，把上一轮的 DNS 修复一起
  冲掉了。**在带未提交改动的文件上做变异测试，必须自己备份、用备份还原**，
  不能用版本控制命令；
- 探测时跑了 `--record`，等于给九个产物写上了「由当前源码构建」的假记录。
  这正是我在同一轮里说过不能做的事。已全部 `git checkout` 还原。
  **一个门禁的 `--record` 如果会写下无法证实的断言，跑它之前必须先确认自己有权做这个断言。**

---

## 64. 绑定层的错误表可以把 Core 的分类改到**另一个层级**去

**现象**：`http://0.0.0.0:8080/` 抛 `InvalidURL`，而 `except ConnectionError` 完全接不住。

**根因**：两张表各自都对，合起来错。

- `core/source/minicronet/error_mapping.h` 把 `ERR_ADDRESS_INVALID` 归入
  `MN_ERROR_NETWORK`（它确实不是格式错误的 URL，是地址不可用）；
- `exceptions.py` 的 `_NET_ERRORS` 把同一个 -108 精化成 `InvalidURL`；
- `InvalidURL` 的 MRO 是 `RequestException → OSError → ValueError`，**不在
  `ConnectionError` 之下**，于是调用方按 Core 的分类写的 `except ConnectionError` 漏掉了它。

**为什么这不是"精化"而是"改层级"**：其余精化都是安全的——`ConnectTimeout` 是 `Timeout`
的子类，`DNSError` 是 `ConnectionError` 的子类。判断标准只有一条：**精化后的类必须是被
精化类的子类**。不是子类，就不是精化，是把错误挪到了另一个 catch 分支。

**requests 也不同意原来的做法**：对 `http://0.0.0.0:1/` 它抛 `ConnectionError`。
所以这一条同时是层级错误和兼容性差异。

**修法**：`-108` 改为 `ConnectionError`。**没改** `-312 ERR_UNSAFE_PORT`（仍是
`InvalidURL`）——端口被禁用确实是 URL 配置问题，与地址不可用不是一回事。

**验证**：直接映射 -108 → `ConnectionError`；真实请求 `0.0.0.0:8080` →
`ConnectionError`/`-102`、`255.255.255.255:8080` → `ConnectionError`/`-109`；
变异把 -108 改回 `InvalidURL` → 1 条红。

**排查方法（可复用）**：把 Core 的 54 条 `case` 与绑定的 70 行逐条配对，然后对每一对
问一句 `issubclass(binding_class, core_class)`。70 行里 11 行映射为 `None`（交回 Core
分类）、证书类走 `IsCertificateError` 兜底、其余逐条核对 —— 最后只有 1 条越级。
**这类分歧不会在任何一边的测试里暴露**，因为两边各自都是自洽的。

---

## 65. 文档说「由 Chromium 负责」，可它负责的那一层 Core 没有编译进去

**现象**：跨域重定向时 `Authorization` 被原样发给新主机。requests 会丢掉它。
实测：`127.0.0.1:A` 上 302 到 `127.0.0.1:B`，B 收到 `Authorization: Bearer probe`。

**根因不是 bug 而是分工**：

- `net::URLRequest` **不做**跨域敏感头剥离。剥离在它上面一层：
  `services/network/url_loader.cc:1025`，通过
  `FollowDeferredRedirect(removed_headers, modified_headers)` 把要删的头传下去；
- Core 用的是 `//net` 的 `URLRequest`，调用点是
  `core/source/request.cc:350` 的 `FollowDeferredRedirect(std::nullopt, std::nullopt)`，
  两个参数都是空的，于是没有任何东西被删掉；
- `docs/COMPATIBILITY_BOUNDARY.md` 却把它写成「Chromium `URLRequest` owns ...
  **sensitive-header removal**」。这句话审阅时看起来是在陈述一个已有保证，
  实际上是把上一层的行为记到了这一层头上。

**为什么绑定层修不了**：`follow=True` 时 Chromium 在 `start_stream()` 里内部跟随，
绑定层拿不到中间跳；`kind == "redirect"` 事件是**跟随之后**才到的通知。
唯一的注入点是手动模式（`allow_redirects=False`），那时根本没有第二跳。
要修只能改 Core：在 `RedirectInfo` 里比较 origin，把 `Authorization` 传进
`removed_headers`。

**处置**：

1. 改正文档：写明「不剥离」、上游剥离在哪一行、Core 的空参数在哪一行、
   以及当下的规避方式是 `allow_redirects=False` 自行重放；
2. 加 3 条测试**锁定当前行为**（跨域保留、同源保留、不跟随则不出门），
   注释里写明「这是已知缺口，Core 开始剥离时这些测试会失败并应当**反转**而不是删除」；
3. 变异验证：模拟「Core 已剥离」→ 跨域那条红。

**教训**：`COMPATIBILITY_BOUNDARY.md` 里每一句「X owns Y」，都要能回答
**「X 在我们编译进来的那一层里吗」**。`//net` 与 `services/network` 是两层，
Chromium 把不少策略放在后者；Core 只编前者，继承的是前者的默认行为，
而前者的默认行为里常常**没有**后者补上的那些策略。

---

## 66. 门禁「为什么还不能迁移」的理由，会随代码漂移而不报错

`tools/audit-layer-ownership.py` 的 `KNOWN` 里 12 条各写着「等下层补上某个缺口再迁移」，
每条都带一个复核指针（文件:行）。这些指针**从不被检查**——门禁只验证「这个模式还在」
和「这条 KNOWN 还有匹配」，不验证指针是否仍指向它说的那段代码。

本轮实测的两处偏差：

| KNOWN 里的说法 | 实际 |
| --- | --- |
| 「reject the **six** fields」 | `_CORE_OWNED_HEADERS` 有 **7** 项 |
| `core/source/request.cc:245` | 那是 `headers.SetHeader(name, value)`，不是拒绝点 |
| `core/source/request.cc:275` | 那是循环的收尾 `}`，不是删除入口 |

三处都不影响门禁判决（模式确实还在、条目确实有效），但**会让按它干活的人找错地方**。
一条「等 X 修好就能迁移」的记录，如果 X 的位置是错的，它就只是一句安慰。

**已改成**：`core/source/request.cc:187-246`（`Request::StartOnNetworkThread` 的整个头部
处理段，拒绝逻辑要插在循环之前）与 `core/source/request.cc:276-279`
（`MergeFrom` 到 `SetExtraRequestHeaders` 这四行，按名删除要加在这里）。
两处都已逐行核对命中。

**可推广的判据**：一条「暂缓」理由里出现的**数量和位置**，都是可以验证的断言，
和文档里的其他断言一样会腐坏。数量要能从代码里数出来，位置要能一眼看出指向什么；
`X:245` 指不到东西的时候，写 `X:187-246（函数名 + 干什么）` 比写行号更耐久。

---

## 67. ABI v12：两件正确性修复，加一个如实标注 0.38% 的零拷贝选项

**这次版本升级承载三件事，收益与代价都写在头文件里。**

### 1. `mn_request_redirect_fn` 带结构化头字段

重定向跳转的 `headers` 原始块此前是绑定层唯一还得自己 split 的地方——`on_response`
早就拿到了 `mn_header_t` 数组。现在两者一致，Python 侧不再重解析（`_headers_from_fields`
取代 `parse_raw_headers` 的那条路径）。重复的 `Set-Cookie` 与混合大小写头名实测正确：
`["a=1", "b=2"]` 两条都在，`X-MiXeD-Case` 归一。

### 2. 跨域重定向的 `Authorization` 剥离（opt-in）

上一轮查明：`//net` 本身不做这件事，剥离在 `services/network/url_loader.cc:1025`，
Core 调 `FollowDeferredRedirect(std::nullopt, std::nullopt)` 所以什么也没删。v12 让 Core 在
`OnReceivedRedirect` 里比 origin 并调 `RemoveRequestHeaderByName`——`is_redirecting_` 是
`URLRequest` 允许该调用的唯一窗口（`net/url_request/url_request.cc:259`）。

**默认关闭**，因为 ABI 11 调用方从未得到这个行为，静默改变比保留缺口更糟。实测：

| 配置 | 目标主机收到 |
| --- | --- |
| 默认 | `Bearer probe` |
| `strip_authorization=True` | `None` |

同源跳转始终保留，即使开启了剥离。

### 3. `MN_READ_TAKE_BUFFER` + `mn_request_release_buffer`（opt-in）

`on_body` 返回这个 disposition 时，Core 把那一块缓冲的所有权交出去，调用方用新导出
函数归还。这样绑定层可以包住字节而不拷贝。

**收益如实标注：实测 0.38%**（64 MiB 本地流，`0.4454 s` → `0.4437 s`）。头文件里写的是
“about 0.7%”，实测更低，因为拷贝本来就是 1.6% 的一部分，而本地传输占 95%。这个选项的
理由是**内存行为**（大 body、不能阻塞的回调），不是吞吐。

**为什么 Rust 侧不能直接把 Core 缓冲变成 `Vec`**：Core 用 Chromium `partition_alloc`，
Rust 用系统分配器。接管那个指针并让 Rust 释放，等于用错误的分配器释放内存。所以
`TakenChunk` 保持裸指针视图，在 `Drop` 里调 `mn_request_release_buffer`。

**实测无泄漏**：15 条 8 MiB 接管流后 fd 13 → 13，RSS +172 KiB，会话仍可用。

### 交付清单

- 头文件：版本 12、回调签名、新字段、新 disposition、新导出函数（29 个符号）
- `core/abi/ABI_VERSION`、Rust FFI 常量与布局、三个导出表、`audit-abi.sh` 的清单
- Core：重定向字段 + 剥离 + 接管/归还；`RequestState` 新增两个字段并附 `Send` 的安全论证
- Rust：`TakenChunk` / `BodyChunk`、`take_body_buffers` 开关、3 条单元测试
- Python：`strip_authorization`、`take_body_buffers`、stub、verb 签名表、4 条回归
- 变异测试：把剥离改成默认开启 → 2 条红

### 尚未完成的一环（必须写明）

`core/binaries/` 下**九个产物仍是 ABI 11**。本机只能编 linux-x86_64，替换单个目标会让
跨平台产物不一致。因此：

- 用 `core/binaries/` 里的旧库跑，v12 绑定会报 `InvalidArgument`（版本校验生效）
- 用新编的 `libminicronet.so` 跑，542 条测试全过、全部门禁绿
- `audit-core-binaries.sh` 的 `permitted = {EXPECTED_ABI, EXPECTED_ABI - 1}` 允许这个
  中间状态，但 `audit-core-source-fingerprint.py` 会红——它如实报告产物早于源码

发布前需要一次全平台重建 + 重新记录 build-args 快照 + 刷新 manifest。

---

## 68. 门禁解析错了，我差点据此「修复」一个本来就是正确的产物

**现象**：九个目标重建为 ABI 12 后，`audit-core-binaries.sh` 报 `macos-x86_64:`
`missing public symbol mn_request_release_buffer`。Linux 与 Windows 全绿，只有两个 macOS
目标红。

**我做了什么，以及为什么都错了**：九轮排查，每一步都排除一个假设——清 `thinlto-cache`、
移除 `-dead_strip`、给函数体加独特分支对抗 ICF、`rm -rf obj/minicronet` 全量重编、
把条目移到导出列表首行、给定义补 `MN_EXPORT`。**全部无效。**

**真正的一步**是让 LLVM 自己回答：

```
$ llvm-objdump --macho --exports-trie libminicronet.dylib | grep release_buffer
0x00035CFB  _mn_request_release_buffer
```

**产物从第一次构建起就是对的**（29 个导出）。错的是 `tools/core-exports.py` 的 trie
遍历。

**根因**（一个字节）：终端节点的结构是 `terminal | flags | address | child_count | edges`，
而代码在读到 terminal 后只取了 `flags`，把 `_after` 丢掉，**用旧的 `offset` 去读 child
`count`**——于是 children 从 flags 字节处开始读。普通终端节点没有子节点，读错也不影响
（后面本来就没有内容）；但**既是终端、又是更长名字前缀**的节点会把整棵子树读歪。

真实数据：`release` 是 `release_buffer` 的前缀。原始字节 `... 01 5f 62 75 66 66 65 72`
——`01` 是「我是终端」，紧接着 `_buffer` 是它的子节点。旧代码把 `01` 当成了「1 个子节点」，
于是走进了 `_buffer` 的边缘字符串里。

**为什么只有 macOS 中招**：trie 是 dyld 的格式。PE 用导出目录、ELF 用动态符号表，都不经过
这段代码。一个只在三个容器之一上执行的解析分支，其正确性没有被任何东西检查过。

**修法**：读到 terminal 后，先 `_uleb` 取 flags，**把 `offset` 推进到 payload 之后**；
再按 `flags & 0x10` 判断是否有 address ULEB 要跳过；然后才读 child count。

**保护**：`tools/core-exports.py --self-test` 用手工构造的 trie 钉住「终端兼前缀」这个形状，
并由 `audit-core-binaries.sh` 在信任任何产物**之前**先跑它。变异测试：把修复改回旧写法，
自检从 0 变 1，真实 dylib 的导出数从 29 变 **0**（不是变少，是整个遍历崩掉）。

**教训**：

- **门禁报错时，先证明门禁能读对，再证明产物错了。** 这轮的九次猜测全部花在后者上。
- 「三个容器里只有一个走到这条分支」的代码，等于没有第二双眼睛；给它一个自检，
  成本是三十行，收益是这九轮。
- 我一度准备把门禁改成「允许 macOS 缺符号」。那会让一个**不存在的**不一致永久固化，
  并且把一个正确的产物当成坏的——比沉默更糟。

---

## 69. 体积优化的天花板：这个构建已经压在 88% 之外没有低垂果实

需求是「所有平台二进制精简优化，保证功能不变」。先量测，结论是**现有构建已在优化配置的
上限附近**，可动的只剩边角，而边角的风险高于收益。

### 已有的优化（全部生效）

| 手段 | 状态 |
| --- | --- |
| `optimize_for_size` → `-Oz` | 已开 |
| `is_official_build`（去 DCHECK、去日志） | 已开 |
| `symbol_level = 0` | 已开 |
| thin-LTO | 已开 |
| `-Wl,--icf=all`（相同代码折叠） | 已开 |
| `-Wl,--gc-sections`（去未引用段） | 已开 |
| `-Wl,--lto-O1` | 已开。仓库实测过：O0 多 57,456 字节，**O2 反而大 12,176 字节** |
| `-fno-unwind-tables` / `-fno-asynchronous-unwind-tables` | 已开 |
| `llvm-strip --strip-unneeded` | 九个产物全部已剥离 |

### 实测：strip 已到极限

| 选项 | linux-x86_64 |
| --- | --- |
| `--strip-debug` | 8,659,096 |
| `--strip-unneeded` | 8,659,096 |
| `--strip-all` | 8,658,568（仅多 528 字节） |

`--strip-all` 只多省 528 字节，而它连 `.dynsym` 的局部符号也去掉，
对崩溃回溯有害。不值得。

### 实测：体积分布

| 段 | 字节 | 占比 |
| --- | --- | --- |
| `.text` | 5,276,560 | 61% |
| `.rodata` | 2,384,958 | 28% |
| `.rela.dyn` | 562,320 | 6.5% |
| `.data.rel.ro` | 297,320 | 3.4% |
| 其余（`.eh_frame`、`.bss`、`.data`、符号表等） | ~139,000 | 1.6% |

`.text` 与 `.rodata` 合计 88%。这两块要再压只能改代码或改数据布局。

### 评估过但**没有做**的三件事

**1. 删 `.eh_frame`（29,724 字节，0.34%）**

`-fno-unwind-tables` 已经在用，剩下的 29 KB 是 C++ 异常展开需要的最小集合。
Core 源码里没有 `throw`/`try`，但 `std::vector`、`std::string` 在分配失败时会抛，
去掉后遇到 `bad_alloc` 就是直接 `std::terminate`，而正常路径的 catch 会静默失效。
**为 0.34% 换掉异常安全，不做。**

**2. 合并 72 条 User-Agent 字符串（可省约 5,900 字节，0.07%）**

精确测量：72 条共 8,139 字节，其中 **88 字节前缀 + 14 字节后缀完全共享**，
只有版本串（如 `97.0.4692.99`）不同。改成「共享前缀 + 版本号」可省约 5.9 KB。

但 `merge-all-constants` 只能合并**完全相同**的字符串，合并不了「前缀相同」。
要省这 5.9 KB 必须改 profile 表的生成代码和 Core 的读取路径——**一次数据布局改动，
换 0.07%**。收益与风险不成比例，且 72 条 UA 的字节级正确性是 profile 证据链的核心，
动它等于动最容易出错的地方。**不做。**

**3. 重新链接去掉 `.note.gnu.build-id`（36 字节）**

build-id 是崩溃回溯时定位构建的凭据。36 字节。**不做。**

### 结论

九个产物（6.2–9.6 MB）已经处于 `-Oz` + thin-LTO + ICF=all + `lto-O1` + strip 的组合下。
剩下的空间需要牺牲异常安全、动数据布局或放弃结构化符号表，每一项的代价都超出
0.07%–0.34% 的收益。

**这不是「优化失败」，是「优化已经做完了」。** 如果目标是让产物变小一个量级，
需要的是改变**功能范围**（例如把 72 个 profile 拆成可选的数据包），那是产品决策，
不是构建优化。

---

## 70. 定位结果：`.rodata` 里是四张数据表，`.text` 里没有巨块

用链接器 map（`-Wl,-Map`，不改 `symbol_level`、不影响发布包）做的归属分析。

### `.rodata`：1.73 MB，前四名占 88%

| 来源 | 字节 | 占比 | 能否裁 |
| --- | --- | --- | --- |
| HSTS preload 全表 | 753,998 | 44% | **不能**（见下） |
| Chrome 根证书列表 | 354,464 | 20% | **不能**（见下） |
| `<internal>`（LTO 合并的字符串常量） | 277,440 | 16% | 部分可 |
| ICU IDNA 数据 | 194,064 | 11% | 可 |
| simdutf 表 | 55,536 | 3% | 必需 |
| 公共后缀表 | 52,906 | 3% | 可 |

### `.text`：没有单一巨块

最大符号 20,822 字节（`net::HttpCache::Transaction::DoLoop`），其后依次是
`QuicFramer::ProcessIetfDataPacket`（17,063）、`ssl_client_handshake`（13,313）、
`ZSTD_decompressBlock_internal`（13,154）。全部是 Chromium `net`、BoringSSL、zstd 的
正常组成，**没有任何一个模块值得单独裁**。

### 为什么最大的两块不能裁

`docs/COMPATIBILITY_BOUNDARY.md:8-9` 写着：**「The Core retains the pinned Chromium
defaults for Brotli, Zstd, HSTS preload, DNS caching, connection pools, TLS sessions,
Alt-Svc, the Chrome Root Store...」**——HSTS preload 与 Chrome Root Store 是这份文档
明确保留的对外承诺，裁掉就等于改契约。

而且 `tools/audit-core-payloads.py:13` 用 `trust_anchors (0xca34)` 作为「最锐利的证人」
验证九个产物的 profile payload 逐字节一致。裁掉根证书表，这个门禁同时失效。

### 结论

真要减 1.1 MB（HSTS + 根证书），需要的是**改产品契约**：把这两张表从编译期数据变成
可选的运行时数据源。那会让「单文件分发」变成「库 + 数据文件」，并需要重写
`audit-core-payloads.py`。这是产品决策，不是构建优化。

**定位这一步的价值**：在没有 map 之前，我上一轮把 `.rodata` 的大头猜成了 72 条 User-Agent
（8,139 字节）。实际 UA 连前六名都进不去，真正的大头是两张我根本没想到的 Chromium 数据表。

---

## 71. 零拷贝让回压失效：`TAKE_BUFFER` 的语义里没有「停」

**缺陷**：`take_body_buffers=True` 时，消费者停顿 2 秒，服务端写出了 **57.81 MiB**
（借出模式只有 0.94 MiB）。回压完全消失，队列无上限增长。

**根因**：`MN_READ_TAKE_BUFFER` 的契约只有「接管缓冲，继续读」。Core 收到它就不再看
`read_paused`。而 Rust 侧的 `on_body` 同时做了两件事：

```
let pause = state.body_bytes >= BODY_BUFFER_LIMIT && state.terminal.is_none();
state.read_paused |= pause;
...
Ok((_, true)) => MN_READ_TAKE_BUFFER,   // took 为真时无条件返回 TAKE
```

标志被单方面置位，Core 却被告知继续读——**两边对「停没停」的认知不一致**，而没有任何
东西检查这个不一致。

**修法**：take 只在**队列低于上限时**使用；到上限就退回拷贝并返回 `PAUSE`。

```
fn may_take_buffer(opted_in, body_bytes, has_buffer, length) -> bool {
    opted_in && body_bytes < BODY_BUFFER_LIMIT && has_buffer && length > 0
}
```

两个分支因此互斥：take 要求低于上限，pause 只在达到上限时置位。修后实测 1.00 MiB，
与借出路径持平。

**第一版测试是假的**：我只写了两条断言比较 `BODY_BUFFER_LIMIT - 1` 与
`BODY_BUFFER_LIMIT` 的大小，然后**变异测试通过了**——把守卫从 `on_body` 里删掉，
测试照样绿。因为它断言的是常量，不是决策。改成调用抽出来的 `may_take_buffer` 后，
删掉守卫立刻 1 条红。

**我这一轮犯的两次操作错误**：

- 又一次用 `git checkout crates/minicronet/src/request.rs` 回退，把 ABI 12 在该文件的
  **全部**改动冲掉（`TakenChunk`、`BodyChunk`、`public_handle`、`Send` 声明）。这是我
  第三次犯同一个错误。**在带未提交改动的文件上，永远用备份还原，不要用版本控制命令。**
- 重放时我凭记忆写了个 ABI 里不存在的 `take_body_buffers` 配置字段，编译立刻报错——
  这说明「重放补丁」不能靠回忆，要对着头文件确认每个字段真实存在。

**教训**：一个「接管资源并继续」的原语，如果调用方同时有「暂停」的诉求，两者必须在
**同一个决策点**上互斥，而不是各自置位后指望对方理解。

---

## 69. 门禁测的不是它说自己在测的东西

**现象**：`bindings/python36` 在 0.2.12 上无法发出任何一个请求。

```python
>>> import chrome_client  # 用 cp36-abi3 扩展
>>> chrome_client.Session().get(url)
TypeError: PyEngine.__new__() got an unexpected keyword argument 'host_mapping'
```

而 `tools/audit-python36-artifact.py` 报 `python36 artifact audit passed: ... one live
request`，`review-before-tag.py` 把它记成 `pass`。

**根因（两层，都在门禁里）**：

1. 检查函数的顺序反了。它先 `import chrome_client._python_impl._native`，再改
   `_native.MODULE_NAME = LEGACY`。但 `_native.py` 在模块级执行 `native = load()`，
   `engine.py` 又 `from ._native import native` —— 两处都早已绑定现代扩展。改名字
   发生在装载之后，对已经绑定的对象没有任何影响。所以这项检查从头到尾测的是
   `bindings/python`，报告上写的却是 `bindings/python36`。
2. 共享 facade 与 3.6 扩展的签名已经漂移，而没有任何东西比对过它们：
   - `PyEngine.__new__`：facade 传 11 个关键字（`host_mapping` … `max_sockets_per_group`），
     3.6 扩展只声明 11 个位置参数，一个关键字都没有；
   - `PyEngine.request`：facade 传 11 个位置参数，3.6 扩展只接受 10 个；
   - `PyEvent` / `PyRedirect`：facade 按 6 元 / 5 元解包，3.6 扩展返回 5 元 / 4 元；
   - 事件里的字节：pyo3 0.15 把 `Vec<u8>` 转成 **`list[int]`**，pyo3 0.30 转成 `bytes`。
     共享 facade 的 `b"".join(state.body)` 因此抛
     `sequence item 0: expected a bytes-like object, list found`。

**规则**：**一个门禁必须先证明它拿到的是被测对象，再去测它。** 具体到本仓库：

- 门禁要断言 `facade_engine.native is module`（同一个对象），而不是信任自己刚设的名字；
- 跨 crate 的共享契约（元组元数、参数表、Python 类型）必须有静态比对，不能只靠
  「两边都 import 成功」。`test_session_defects.Py36BindingShapeTests` 现在解析两个
  `lib.rs` 的参数表与元组元数直接比；
- 静态比对要**比声明**，不能比子串。第一版形状测试用 `assertIn("take_body_buffers",
  legacy)`，在参数被删掉、但赋值语句里还留着这个名字的树上照样通过（实测）。

**门禁**：`audit-python36-artifact.py` 现在在 import 之前把扩展登记进
`sys.modules`（两个名字都登记），断言 facade 绑定的就是它，然后跑 sync GET、
重定向、异步 GET 三条路径；`audit-session-parity.py` 新增
`check_options_reach_the_core()`。变异测试：删掉 `platform=` 参数 → 门禁报
`the legacy extension cannot serve a request the facade makes: TypeError ...`；
删掉 `take_body_buffers=self.take_body_buffers` → 门禁报
`never reads self.take_body_buffers`。

---

## 70. 构造选项被接受、然后被丢掉

**现象**：`Session(take_body_buffers=True)` 完全不起作用。

**根因**：构造函数把值存进 `self.take_body_buffers`，`_native_request` 从没读过它。
`engine.request(...)` 的最后一个实参是 `self.strip_authorization`，选项到此为止。

**为什么没有门禁能发现**：这个选项的两种取值**返回完全相同的字节**。它省掉的是 Core
侧的一次 memcpy（实测 0.38%），不是任何可观测的输出。所以：

- 请求测试通过（数据正确）；
- 类型检查通过（属性存在、类型正确）；
- 布局/ABI 门禁无关（选项不跨 ABI，靠 disposition 表达）。

**规则**：**「被接受」不是「被实现」**。一个构造选项必须在某个可检查的地方到达下层，
否则它就是 AGENTS §8 禁止的静默忽略。

**门禁**：`tools/audit-session-parity.py` 的 `check_options_reach_the_core()` 读取
`_native_request` 的函数体（先剥掉注释——第一版没剥，注释里提到了选项名，删掉转发后
门禁仍然是绿的，实测），要求每个登记在 `FORWARDED` 里的选项以 `self.<name>` 的形式
出现在那里。运行时那一半由 `test_session_defects.EngineOptionForwardingTests` 用
一个假 engine 断言它收到的 kwargs。两者都做了变异测试。

---

## 71. `getattr` 的默认值把「字段不存在」伪装成「值为 None」

**现象**：`Response.ech_status` 在同步路径上恒为 `None`，而异步路径正常返回
`"not_attempted"`。同一个 Core、同一个请求，两条路径给出不同答案。

**根因**：同步路径读 `head.ech_status`，而 `PyResponse` 从未声明这个字段。
`models.ech_of` 用的是 `getattr(head, "ech_status", None)`——一个不存在的属性和一个
值为 `None` 的属性在这里无法区分，于是「字段丢了」被读成「Core 说不知道」。异步路径
问的是 request 对象（`self.request.ech_status()`），那里有真 getter，所以一直是对的。

**为什么没有门禁发现**：`ech_status` 当时不在 `models.pyi` 的 `Response` 声明里。
stub 检查只比对「实现有的 stub 也要有」这一个方向，实现少一个字段它不会红；类型检查
也只看被声明的东西。**一个从未被声明的属性，既不缺也不多。**

**规则**：

- 读 ABI 报告的字段时不要用 `getattr(obj, name, None)` 吞掉缺失。要么让类型
  声明存在（`#[pyo3(get)]` / `.pyi`），要么显式区分「没有这个字段」和「字段是
  None」。这两件事在语义上不同：前者是绑定层缺陷，后者是 Core 的真实回答。
- 同步与异步两条路径读同一个事实，必须有测试同时断言两者。只测一条时，另一条可以
  一直错着，而报告上写着「已覆盖」。

**门禁**：`test_session_defects.EchStatusReportingTests` 断言同步路径给出真实值，并断言
两条路径相等。变异测试：把 `ech_status` 在 `PyResponse` 里固定成 `None` → 两条测试都红。

---

## 72. `SameSite` 只匹配小写，服务器发的是 `SameSite=None`

**现象**：任何一个 `Set-Cookie: ...; SameSite=None` 的响应都让整个 `Session().get()`
抛 `ValueError: unknown SameSite value "None"`。不只是那条 cookie 丢掉——请求本身失败。

**根因**：`cookie_same_site` 用 `match name { "none" => ..., "lax" => ... }`，只认小写。
而这个值来自 `http.cookiejar`，它保存的是**服务器写的原样**；浏览器实际发出的正是
`None` / `Lax` / `Strict`（首字母大写）。三个合法值一个都不匹配，全部落到 `other` 分支。

**规则**：从外部协议文本解析枚举时，大小写是协议的一部分，不是格式偏好。
RFC 6265bis 把 `SameSite` 的取值定义为大小写不敏感，所以比较前必须归一。
同一类错误还存在于任何「按字面量匹配头字段取值」的地方。

**门禁**：`test_session_defects.SameSiteCookieTests` 覆盖 `None` / `Lax` / `Strict` /
`none` / `lax` / `strict` 六种写法，并断言未知取值仍然 fail-closed（不能被静默当成
`unspecified`，那会改变上线内容）。变异测试：把 `to_ascii_lowercase()` 去掉 → 四条测试红。

---

## 73. DoH 与 ECH 的实测边界（不是缺陷，是能力边界）

用户问「DoH 和 ECH 支持吗」。实测答案分成两半，值得记下来，因为很容易把两者混为
一谈。

**DoH / DoT / 指定 DNS 服务器：支持，且实测生效。**

| 配置 | 观测 |
| --- | --- |
| `dns_nameservers` 指向本地 53 端口 | 服务端真的收到查询：`dns.google` 的 A(1) 与 HTTPS(65) |
| `dns_nameservers` 指向死服务器 `127.0.0.1:1` | 仍然 200——Chromium 回退到 `TaskType::SYSTEM` |
| `doh_template` + `dns_secure_mode=2` 指向 `127.0.0.1:1` | `DNSError ERR_NAME_NOT_RESOLVED`，**不回退** |
| 同上但 `dns_secure_mode=1` | 200——opportunistic 允许回退 |

最后两行才是 secure 与 opportunistic 的实质区别，也是 `dns_secure_mode` 这个参数值得存在
的理由。第一行同时说明「Core 到底有没有用这个解析器」是可验证的：数查询比看返回值可靠。

**ECH：只发 GREASE，不会真的加密 SNI。** 这是 Chromium 桌面构建的事实，不是本项目的
缺口：

- ECHConfigList 的唯一来源是 DNS HTTPS 记录（`net/dns/dns_response_result_extractor.cc:581`），
  而 `TransportConnectJob` 只有在 destination 是带 scheme 的 `SchemeHostPort` 时才把
  `DnsQueryType::HTTPS` 加进查询（`net/dns/host_resolver_manager.cc:845-852`，需要
  `kUseDnsHttpsSvcb`，默认开启）。HTTPS 记录确实被查了——实测本地 53 端口收到 qtype 65。
- 但公网实测 `cloudflare.com` 的 HTTPS 记录**不含 `ech=` 参数**，所以拿不到 config。
- 没有 config 时 BoringSSL 只发 ECH GREASE：实测 ClientHello 里存在扩展 `0xfe0d`，
  与真实 Chrome 119+ 的指纹一致（`tools/verify-wire-capture.py:117` 的 `ECH_FIRST_MAJOR`）。
- 因此 `mn_ech_status_t` 的 `MN_ECH_FAILED` 不可达，`SUCCESS` 也不可达，实测恒为
  `not_attempted`。头文件已经把这条写在契约里（`core/abi/minicronet.h:596-605`）。

**结论**：`Response.ech_status` 现在能如实报告 `not_attempted`（修之前同步路径报 `None`），
但不要据此期待 ECH 真的生效——在这个 Chromium 版本上它不会。

---

## 74. 同一个「支持 ECH」的结论，在三个版本里由三套不同机制实现

**问题**：不同 Chrome 版本的 DoH 和 ECH 支持有什么不同。

**ECH 的边界是 119，机制换过三次但行为没变**：

| 版本 | 开关 | 位置 |
| --- | --- | --- |
| 110–121 | `config().EncryptedClientHelloEnabled()` | `net/base/features.cc` 的 `kEncryptedClientHello` |
| 122–156 | `config().ech_enabled` | feature 已删除 |
| 157 | `SSLClientContext::GetEchMode()` | `net/socket/ssl_client_socket.cc` |

110/117/118 的 feature 是 `FEATURE_DISABLED_BY_DEFAULT`，119/120 是 `ENABLED_BY_DEFAULT`，
122 起整条删除。这与抓包边界逐版本吻合：118 的 6 条连接都没有 `0xfe0d`，119 起 6 条全有。

**教训**：只查一个版本的源码然后外推，会得到一个「机制」而不是「行为」。这次三套机制
的行为一致，所以外推碰巧没错；但如果是 122 那次 feature 删除，只看 119 的源码会以为
`kEncryptedClientHello` 仍然存在。**版本边界必须逐 tag 核对，而不是从相邻版本继承**
（AGENTS §9 的同一原则）。

**GREASE 与真实 ECH 的判据**：payload 长度在 372/436/500/564 之间随机浮动就是 GREASE，
真实配置会在六条连接里稳定。`tools/verify-wire-capture.py` 已经把这个判据写成断言：
从 119 起每条连接必须有扩展，且 6 条 payload 必须互不相同。

**DoH 没有版本边界**：86 起每个 profile 都支持，只是 `SecureDnsMode` 的声明位置从
`net/dns/dns_config.h` 移到了 `net/dns/public/secure_dns_mode.h`。

**一处差点写错的**：我先写「`insecure_dns_mode` 在 152 之前不存在」，逐 tag 核对
`net/dns/host_resolver.h` 后发现是 **156**：86/95/118/130/152 都没有该字段，156/157 有。
差一个版本号看起来无关紧要，但它决定了「这套注入点能不能用在更早的 Chromium 上」，
所以文档里改成了逐 tag 列出的具体版本。**否定性结论同样需要证据**（AGENTS §11）。

---

## 75. `dns_over_tls` 不是「用 DoT 解析」，是「关掉内置解析器」

**现象**：README 把 DoT 标成 ✓。实测 `dns_over_tls=True` 配 `dns_nameservers` 指向本地
UDP 端口时，服务端收到 **0 个查询**（TCP 也 0 个），而请求照样返回 200。

**根因**：Chromium 桌面版**根本没有 DoT 客户端**。`net/dns/` 里只有明文的
`dns_tcp_attempt`，没有任何地方消费 `DnsConfig::dns_over_tls_hostname`，853 端口在
整个解析器里一次都不出现。`dns_over_tls_active` 的语义是**闸门**，不是传输选择：

```cpp
// net/dns/dns_client.cc:168
case InsecureDnsMode::kEnabledBuiltIn: {
  const DnsConfig& config = GetEffectiveConfig();
  return !config.nameservers.empty() && !config.unhandled_options &&
         !config.dns_over_tls_active;   // <- 置 1 就返回 false
}
```

内置解析器因此拒绝每一个 job，`TaskType::SYSTEM` 接手；`dns_nameservers` 与
`doh_template` 全部被丢弃。**调用方要求加密解析，静默拿到明文系统解析。**

**实测对照**（本地 UDP 探针数包）：

| 配置 | 到达配置服务器的查询 | 结果 |
| --- | --- | --- |
| `dns_nameservers` | 4 个（A(1) + HTTPS(65)） | 200 |
| 同上 + `dns_over_tls=True` | **0 个**（UDP 与 TCP 都是 0） | 200（系统解析） |
| 同上 + `dns_rotate` | 4 个 | 200 |
| 同上 + `dns_padding` | 4 个 | 200 |

**修法**：绑定层与 Core 各拒绝一次，`MN_ERROR_INVALID_ARGUMENT` /
`UnsupportedFeature`，并在错误信息里指向能用的替代（`doh_template` +
`dns_secure_mode=2`）。ABI 头文件相应把该字段改成「必须为 0」。

**为什么这是 fail-closed 而不是功能缩水**：AGENTS §8 的承诺是「无法用 Chromium
忠实实现的选项必须抛错」。DoT 正是这种情况——不是没实现，是上游没有可用的实现，
接受它只会改变一个与它同名但语义相反的东西。

**教训**：一个选项「被传给下层」不等于「下层做了这件事」。`dns_over_tls` 一路传递
到 `DnsConfigOverrides::dns_over_tls_active` 完全正确，错的是**那个字段的语义**。
判断一个开关是否真的生效，唯一可靠的方法是**数下层的可观测行为**（这里是 DNS 包），
而不是看它有没有被写到某个结构体里。

## 76. 同一个缺陷模式在同一张表里出现了两次

修完 `dns_over_tls`（LESSONS 75）之后，我用同一把尺子量了它旁边的一格，
`dns_padding` 立刻掉出来：

| 选项 | ABI 接收 | Core 传给下层 | 下层真的做了吗 |
| --- | --- | --- | --- |
| `dns_nameservers` | ✓ | `overrides.nameservers` | ✓ 数到 4 个查询 |
| `dns_rotate` | ✓ | `overrides.rotate` | ✓ |
| `dns_over_tls` | ✓ | `overrides.dns_over_tls_active` | ✗ 反而是**关掉**内置解析器 |
| `dns_padding` | ✓ | `options.dns_padding`，**到此为止** | ✗ `engine.cc` 一次都没读过 |

四个选项里两个是假的，而 README 当时把四个都标成 ✓。

**规则**：修一个「被接受但没生效」的选项时，**先把同一组里每一个选项都数一遍**。
它们共享同一条传递路径，所以同一个断点往往不止掐断一个值——`dns_padding` 断在
`EngineOptions` 与 `engine.cc` 之间，而 `dns_over_tls` 断在下层的语义上，两个断点
不同，但都藏在这张表的同一行附近。

**判据（可执行）**：对每个选项问「我在下层能数到什么」。数得出来的才算 ✓，
数不出来的要么修，要么改成抛错。**「它被写进了某个结构体」不算证据。**

## 77. fail-closed 的守卫挂在哪一层，决定了它有没有用

`_HOP_BY_HOP_HEADERS` 拒绝 `Connection` 等六个字段，测试也全绿。但守卫挂在
`request()` 的 `_prepare_call` 里，而 requests 的契约里还有两条路能到达同一张网：
`session.headers["Connection"] = "close"`（会话级 header 在 `prepare_request` 里
才合并进来，那时 `request()` 早已跑完），以及 `prepare_request()` + `send()`（或
由调用方手工构造 `PreparedRequest` 直接 `send()`）。实测三条路都把五个字段原样发到
了 HTTP/1.1 的网线上——正是这个守卫存在的理由。

**规则**：fail-closed 的检查必须挂在**所有入口的汇合点**，而不是「主要入口」。
判断方法是把「值最终落到哪」倒着走一遍：这里所有路径都经过 `Session.send` /
`AsyncSession.send`，所以检查移进两个 `send()` 才算完成。

**同一个缺陷的第二个形状**：`headers={"Cookie": None}` 静默失效。请求级删除被
`prepare_headers` 用 `if value is None: continue` 丢掉了，删除意图没有留下任何
记录，守卫看到的是一个「对 Cookie 没有意见」的请求。修法不是再加一条判断，而是让
`Headers` 的删除记录（`deleted_names`）穿过 `prepare`——**丢弃意图本身**就是那个
缺陷。这里还踩了一个自己的坑：`Headers(headers) if headers else Headers()` 里，
只含删除记录的对象没有条目、是 falsy，于是记录又丢了；必须是 `is not None`。

## 78. 请求级参数被写进会话级存储，就等于把作用域改成了会话

`cookies={"per": "P"}` 的实现把 cookie 写进引擎的 `CookieMonster`。存储不是请求
作用域的，写进去就再也没人收回：实测 `get(url, cookies={"per": "P"})` 之后的
一个普通 `get(url)` 仍然发出 `Cookie: per=P`，而 requests 不发。

修的时候有两个反直觉之处，都是先按直觉写错、被测试抓住的：

1. **删对不上的三元组等于没删。** 空 domain / 空 path 由 `cookielib` 记录，store
   归一化成「请求 host + `/`」（`net/cookies/canonical_cookie.cc`）。按调用方给的
   名字删是静默 no-op——和要修的缺陷是同一个形状，只是换了位置。
2. **只删不还原，会把会话 cookie 删掉。** `session.cookies.set("a", "1")` 之后
   `get(url, cookies={"a": "2"})`，删完 store 是空的而 jar 里还是 `a=1`——jar 与
   store 分叉，正是这个模块存在的目的所反对的事。必须**写入前读走将被覆盖的行，
   请求结束后写回**。

**规则**：给某个参数实现「只影响这一次」时，先问它的落点是不是共享的。落点是会话级
存储时，「临时写进去再删掉」需要额外回答三个问题：删的条件与写入的归一化一致吗、
被覆盖的旧值怎么办、异常与重定向路径都覆盖到了吗。三个问题里任何一个没有答案，
那就不该写进共享存储。

**顺带的一条**：`discard_cookies=True` 与显式 `cookies=` 是互斥的两个承诺
（「一个都不发」对「这一次带这个」），原来的实现选了前者且不报错。按 AGENTS §8
应当抛错。但**字面 `Cookie` 头**不能一起拒——那一侧已经被
`DiscardDoesNotRewriteTheCallersRequest` 钉死为「discard 赢，且不改写调用方对象」
（字段从副本上 drop），把两种写法一起拒会破坏既有契约。**要拒的是「丢了就再没有
等价表达」的那一种，而不是「看起来同类」的全部。**

## 79. 把请求级状态写进共享存储，单线程测试永远发现不了

`cookies={"k": "v"}` 的实现是「写进引擎的 store，请求结束后删掉」。单线程跑
一遍，行为完全正确；加并发立刻错：6 线程 × 15 请求，每个线程带自己的
`cookies={"k": <序号>}`，**90 个请求里有 1 个带着别的线程的值**发出去了。

原因不是「删得不够快」——是**写入本身就撞了**。两个请求在任一请求开始前都已经
把 `k` 写进同一个 store，后写的赢，先写的那个请求随后用着别人的值发出去。
事后清理对这个时间窗无能为力。

**判据**：给某个参数实现「只影响这一次」之前，先问它的落点是不是共享的。
落点在进程或会话级存储上时，「临时写、用完删」只是把作用域改了个名字，
真正的隔离必须来自**新的存储实例**（这里是独立引擎）。

**顺带的两条**：

- **归一化过的三元组不能靠调用方拼。** 空 domain/path 由 `cookielib` 记录，
  store 归一化成请求 host + `/`（`net/cookies/canonical_cookie.cc`）。按调用方
  给的名字删是静默 no-op——和要修的缺陷同一个形状，只换了位置。
- **「恢复旧值」会和服务端打架。** 为了不让临时覆盖删掉会话 cookie 而加的回写，
  在响应同时 `Set-Cookie` 覆盖了同名 cookie 时，会把服务端刚设的值盖回旧值，
  jar 与 store 就此分叉。服务端有最终话语权，回写前必须确认 store 里还是自己写的那个值。

## 80. 一个占位符被当成声明使用了很久

`create_cookie` 给每个 cookie 塞 `{"HttpOnly": None}`（requests 也这么写），而
`has_nonstandard_attr("HttpOnly")` **只问键在不在**。于是 `session.cookies.set()`
造出来的每一个 cookie，在镜像进 Core store 时都自称 `HttpOnly`。

这不是显示问题。Chromium 拒绝把 `HttpOnly` 降级为非 `HttpOnly`，所以
`Set-Cookie: a=NEW` 想覆盖一个 caller 自己设过的 `a` 时，整次写入被拒——
表面上看是服务器行为异常，实际上是本地把一个占位符当成了声明。

**规则**：占位符与声明如果共用同一个数据结构，就必须有办法区分它们；
`None`/`False`/空串在「键存在」这类判断里是不可见的。改之前先确认这个字段的
两个写入方（`create_cookie` 与 `morsel_to_cookie`/`extract_cookies`）写进去的值
在语义上是否相同——这里两者都写 `None`，唯一区别是键存不存在，而我第一版修法
正好判反了方向。

## 81. 「更聪明」的默认值是一种指纹，也是一种攻击面

`files={"f": ("a.html", b"<script>…")}` 里的 2 元组只说了「字段 f，文件名
a.html，内容这些字节」。facade 却调用 `mimetypes.guess_type()` 给 part 补了一个
`Content-Type: text/html`——requests 与 curl_cffi 都不补，只在你显式传第 3 个
元素时才发这一行。

实测（服务端按收到的 part 类型回报）：

| files= | 修前 | requests |
| --- | --- | --- |
| `("a.txt", b"xy")` | part `text/plain` | 无 part 类型 |
| `("a.html", b"<script>x</script>")` | part **`text/html`** | 无 part 类型 |
| `("a.bin", b"xy")` | part `application/octet-stream` | 无 part 类型 |

**后果不是格式差异**：把附件存进对象存储、再把 part 类型原样回显的服务端，
从这一刻起把调用方上传的 HTML 当 HTML 提供出去了。第二个凭空发明的断言是
`application/octet-stream`——对每个不认识的后缀都这样标，同样是一句调用方没说过的话。

**判据**：给一个可选字段补默认值时，先问「这个值是谁的断言」。文件名只是文件名，
它不是类型声明；`RFC 7578 §4.4` 让 part 类型可省略，所以省略才是既兼容又互操作的选择。
「猜得更准」在协议层面不是优点——多出的每一个字节都是对方能观测到的指纹，也是
调用方没授权过的语义。

## 84. 两个「环境性失败」的根因都是我们自己的判断方式

**现象**：`python3 tools/audit-abi-layout.py` 长期报 `collect2: fatal error: cannot find
`'ld'``，`tools/audit-pypi-metadata.sh` 长期报 `docs/CHROME_158_SUPPORT.md:47:
commit ea9f3a3… is not the pinned CHROMIUM_REVISION`。两次都被归因成「本机环境问题」
并在交付摘要里当成既有状态放过。

**第一个根因（gate 自己）**：`resolve_compiler()` 用 `shutil.which` 找到编译器就认为能编译，
但 `subprocess.run` 没有传 `env`，子进程继承的环境**可能根本没有 `PATH`**。实测本会话
`env` 只有 `PWD`、`SHLVL`、`_` 三个变量；`gcc` 派生的 `collect2` 用 `execvp("ld")` 查 PATH，
于是明明 `/usr/bin/ld` 存在也找不到。`shutil.which` 只证明「这个进程的 PATH 上有它」，
**没有把那条 PATH 交给子进程**。修法是 `compiler_env()`：把编译器目录与三个标准目录补进
子进程 PATH。注意空字符串 PATH 与未设置 PATH 在 `execvp` 下语义不同，所以必须发非空值。

**第二个根因（gate 的排除粒度）**：`audit-pypi-metadata.sh` 只按**目录**排除 `profiles/`，
理由写得很清楚——那里的 `chromium_commit` 是「抓包所用 tag 的 tree」，与
`CHROMIUM_REVISION` 是不同的事实。但同一个事实类型也存在于 `docs/CHROME_*_SUPPORT.md`：
`boringssl_revision` / `quiche_revision` 来自该 release tag 自己的 DEPS，**任何 BoringSSL
commit 都不可能等于 CHROMIUM_REVISION**。按目录排除会把整份验收记录一起豁免，所以改按
**表格行**排除：一行同时指出字段名（`boringssl_revision`/`quiche_revision`/`chromium_commit`）
并说明「来自该 tag 的 DEPS」才允许出现外来 hash；裸 hash 仍然红。

**规则**：把失败归因为「环境」之前，要先能说出**是哪一行代码**让它依赖了那个环境。
说不出来就不是环境问题。这两次都能指出：`subprocess.run` 少传 `env`；
`name.startswith("profiles/")` 只覆盖了载体的一种摆放方式。

**变异测试**（两项都做了）：

| 变异 | 期望 | 实测 |
| --- | --- | --- |
| `env=dict(os.environ)` 退回原样 + `env -u PATH` | 非零 | `exit 1`，`compilation terminated` |
| 把 DEPS 行改成裸 `\| 某个 commit \| <hash> \|` | 非零 | `exit 1` |
| 字段名保留、删掉「来自 DEPS」说明 | 非零 | `exit 1` |
| 恢复原样 | 0 | `exit 0` |

## 85. 一个 scheme 的支持与否，不是绑定层能回答的问题

`quic://` 在 Python facade 里被硬编码成 `UnsupportedFeature`，注释写着
`ENABLE_QUIC_PROXY_SUPPORT is off`。这句话在写下的那天是对的，在一个月后就成了
**绑定层替 Core 决定能力**——而它根本不知道产物是用哪份 `args.gn` 编的。

三件事同时错了：

1. **答案被写在了错误的一层。** 换一个 Go 绑定，同一句判断要再写一遍，而且很可能
   写成相反的结论。按 §1.1 的主判据问一句「换语言要重写吗」，答案是「要」，就应该
   下沉——但正确的下沉不是「搬到 Rust」，而是**让 Core 自己报告**：
   `mn_engine_capabilities` 返回 `MN_CAPABILITY_QUIC_PROXY`，绑定层只读位。
2. **失败方式不可区分。** 没有能力位时，唯一的探针是「发个请求看错误码」，而
   `ERR_NO_SUPPORTED_PROXIES (-336)` 同时意味着「本构建不支持这个 scheme」和
   「支持但你指定的代理拒绝了」。两件完全不同的事共用一个错误码，调用方无法据此
   决策。这不是错误码不够用，是**信息根本没被送出来**（§1.1 判据 1）。
3. **`proxy=` 与 `proxy_chain=` 走了两条不同的解析路径，并且只有一条被改。**
   `proxy_chain` 经 `ProxyServer::FromSchemeHostAndPort`，`proxy=` 经
   `ProxyRules::ParseFromString(rules)` —— 后者的 `is_quic_allowed` 默认 `false`。
   改完第一条路径后 `proxy=quic://` 仍然返回 `-336`，而 `proxy_chain=quic://` 已经
   发出 QUIC 包。两个选项描述同一条连接路径，表现不一致本身就是一个必须修的缺陷，
   而不是「两个选项语义不同」。

**变异测试暴露的另一件事**：把 `tools/build-core-*.sh` 里新加的两个 gn 变量写进
`record-core-build-args.py` 之前，快照里没有任何东西记录它们——也就是说有人把
`enable_quic_proxy_support` 改回 false 重建，所有静态门禁都还是绿的，只有到第一个
`quic://` 请求才会发现。配置快照的价值在于记录**能改变能力**的开关，不是记录所有开关。

**验收的两个级别要分开说。** 解析层（`quic://` 能归一化、未知 scheme 仍被拒）和握手层
（UDP 上真的出现 6 个 QUIC v1 Initial 包）都在本机做到了；**隧道本身没有验证**，因为
没有可用的 QUIC 代理对端。把这件事写成 T-8 而不是写进「已支持」，是因为
`MN_CAPABILITY_QUIC_PROXY` 断言的是「这个产物能拨」，不是「拨通过」。

## 86. 校验写完之后，必须先证明它会红

这一轮补「同步链缺一个校验」，我写了三版，前两版都是**永远为真**的检查，而且两次
都是变异测试把它们照出来的：

1. **在 `install` 之后比对目标树**。变异「删掉目标文件」→ exit 0。因为 `install`
   先跑，会把目标修好，校验再去比，比的已经是修好的那个。这个检查唯一能抓的是
   「install 指向了错误路径」，而那种情况 install 本身就会失败。
2. **用 `case` 列出扩展名，判断每个源文件是否被覆盖**。变异「删掉 `*.h` 的 install
   循环」→ exit 0。因为 `case` 里那份清单是**我自己写死的**，它不知道规则被删了；
   它重述的是我脑中的假设，不是脚本的实际行为。
3. 第三版从脚本自身的 `for` 行与 `install -m 644` 行里抽出 glob，再拿它匹配源树。
   两个变异都红了。

教训不是「要写变异测试」——那条规则早就在 §6 里。教训是：**一个检查的判据必须来自
被测对象本身，而不是来自写检查时的记忆。** 第二版看起来完全合理，`case` 分支写得
清清楚楚，注释也解释了为什么这些扩展名要覆盖——它错在把「我知道 sync 装什么」当成
了「sync 装什么」。

同一轮里，`tools/audit-core-inputs.sh` 的第 2 条不变式也犯了这个错：它的 glob 清单
（`*.c` / `*.cc` / `minicronet/*.h`）漏了 `core/source/*.h`，于是新加的
`platform_identity.h` 既不被检查是否被跟踪，也不影响它打印「all tracked」。补上 glob
后它立刻报出那个文件未被 Git 跟踪——一个真实存在、此前无人发现的问题。

**判据**：新增或修改任何门禁，先问一句「把被检查的东西破坏掉，它会红吗」，并且真的
去破坏。变异测试跑出绿色不是麻烦，是这套方法唯一的价值。

## 87. 同一个事实的两份实现，即使同语言也算缺陷

把平台身份下沉到 Core 时，我在 `minicronet.cc` 写了 `PlatformHintLiteral`，而
`engine.cc` 里早就有一个功能完全相同的 `Engine::platform_hint`。两者都是 `switch`、
都返回 `"Windows"` / `"macOS"` / `"Linux"`，都带同样的 `BUILDFLAG` 兜底。

AGENTS §1.1 的主判据问「换一个语言重写，这段代码要不要再写一遍」，用在这里会漏判：
两份实现都在 C++ 里，看起来不像「跨语言重复」。但那条判据追问的第二句是
「两个实现出现分歧时，谁是错的」——答案需要回去看一遍源码才知道，这就说明它不该有
第二份。

结果是抽成 `core/source/platform_identity.h`，`PlatformToken` 与 `PlatformHint` 各
一份，三个调用方（`DefaultUserAgent`、`Engine::platform_hint`、两个 ABI getter）全部
引用它。验证方式是端到端比对：ABI 报告的值与线上字节逐字相同。

## 88. 平台身份的派生点不止一个，而在同一次运行里可以同时活着

`browser_headers.py` 用自己的 `sys.platform` + `platform.machine()` 派生 User-Agent
组与 `sec-ch-ua-platform`；`core/source/minicronet.cc` 用 `BUILDFLAG(IS_WIN/IS_MAC)`
+ `ARCH_CPU_*` 派生同样的两个字符串。而 `browser_headers.py` 的模块注释里写着
「Must match `core/source/minicronet.cc` exactly」——也就是说这个风险是已知的、写在
代码里的，只是从没有人核对过。

它在本机「碰巧对上」，因为两台机器都是 Linux x86_64。反例是现成的：同一台 x86_64
主机加载 `core/binaries/linux-x86/`（32 位构建）时，`ARCH_CPU_X86` 让 Core 说
`X11; Linux i686`，而 `platform.machine()` 仍返回 `x86_64`，Python 说
`X11; Linux x86_64`。两条路径当场分叉，而两边看上去都是对的。

修法不是「把注释写得更醒目」，是把派生下沉到 Core：ABI v14 新增
`mn_platform_user_agent_group` 与 `mn_platform_hint`，Python 只读不推。读不到时抛
`UnsupportedFeature`，不回退到本地猜测——猜测对了这台、错了下一台，正是要消除的东西。

顺带发现一条「看起来在验证」的测试：`test_the_platform_token_matches_the_cores_derivation`
名字声称与 Core 一致，实际只断言了前缀和没有括号。已改成逐平台比对 ABI 返回值，
并用变异测试证明伪造平台值会让它红。这是 §7「不得写看起来在验证的注释」的又一例。

## 89. 发布身份是一次集合差，「不再推那个远端」并没有撤销一个发布者

0.2.9 由 `dafatyu/chrome_client` 的 `build-historical-releases.yml` 发布，
0.2.16 由 `komAAmok/chrome_client` 的 `build-wheels.yml` 发布，两者相隔
**23 小时**。PyPI 因此在 0.2.16 上挂出了 Change of provenance。

一开始的直觉是「把旧仓库的推送停掉就好了」。这个直觉错在以为发布者是一个**配置**：改掉
就生效。实际情况是 PyPI 只比较两个**集合**，集合里没有「上一次是谁」这种东西：

| 事实 | 复核位置 |
| --- | --- |
| 比较的两份集合是 `PublisherSource(kind, repository)` 与 `PublisherWorkflow(kind, repository, workflow)`，`url` 被显式排除（它每个 release 都带 commit sha） | `warehouse/attestations/models.py` 的 `PublisherSource` / `PublisherWorkflow` |
| 任一侧多一个或少一个就是 `CHANGED_PROVENANCE` | 同文件 `ProvenanceStatus.states()` |
| 比较对象是**前 14 天内最近的、有 provenance 的**那个 release | `warehouse/packaging/models.py` 的 `PROVENANCE_COMPARISON_WINDOW_DAYS = 14`、`Release.comparison_provenance_release()` |
| 窗口锚在**被比较 release 自己的** `created` 上 | `Project.provenance_statuses()` 传的是 `created - window` |

第四条是这里最反直觉的一处：告警不是「现在状态」，是**那个 release 的历史判定**。0.2.16
的窗口固定是它上传前的 14 天，所以这条告警不会随日历翻页消失；删掉 0.2.9 也不够——
0.2.7–0.2.15 全部落在这个窗口里，而且全是同一个旧身份，下一个候选会立刻顶上来。

真正让人停下来的是第二条路：把 0.2.16 删了重传、不带 attestation。那不是消除告警，是把
`CHANGED_PROVENANCE` 换成 `LOST_PROVENANCE`（`files_with_provenance == 0`
而 `comparison.counts.files_with_provenance > 0`），后者读起来更接近「被接管」。
而且 attestation 记在 append-only 的透明日志里，本来就没有事后修改这一说。

所以修法只能是往前看：

1. 仓库侧删掉第二条发布路径（`build-historical-releases.yml` 的 `pypi` job），
   并给 `build-wheels.yml` 的 publish job 加 `github.repository` 守卫——一个
   fork 发同一个项目不是「同一个发布者」，是第二个发布者。
2. `tools/sync-remotes.sh` 删除：规则说只推 `origin`，脚本还在推 `backup`，
   这条路径存在的唯一效果是让「只有一个远端」变成一句注释。
3. PyPI 侧删掉 `dafatyu/chrome_client` 的 trusted publisher。仓库里删干净不等于它
   失效：只要那条 publisher 还注册着，它就是这个项目的合法发布身份。
4. 0.2.16 页面上那条告警留着。它记录的事实是真的——发布者确实换了，而且换成了正确的那个
   （`komAAmok`）。项目页顶部的横幅跟随最新 release，下一次同身份发布就消失；
   release 页那条是历史，不是待修缺陷。

**判据**：任何「换 CI、换仓库、换 workflow 文件名」的动作，先问一句——14 天内有没有另一个
release，它是谁发的？两句话能回答，就不会再产生一次不可撤销的告警。

## 90. 一个 Python 包、两个扩展：改一边就是改一个还不存在的地方

0.2.16 的 cp36-abi3 轮子装到任何解释器上，只要 session 带了 `impersonate=`，第一个请求
就失败：

    AttributeError: type object 'builtins.PyEngine' has no attribute 'platform_identity'

被 `browser_headers._platform_identity` 捕获后包成 UnsupportedFeature，消息说「Core 不支持
mn_platform_user_agent_group (ABI v14)」。那句话是错的：Core 的 manifest 写着
`abi_version: 14`，符号也在。真正缺的是 `bindings/python36/src/lib.rs` 里的一个方法——
ABI v14 下沉平台身份时，`platform_identity` 只加进了 `bindings/python` 那个 crate。

两个轮子发的是**同一个 Python 包**，共享**同一份 facade**，所以「一个方法只加在一边」不是
半成品，是一个会在另一个轮子上 100% 复现的缺陷。而当时没有任何东西检查这件事。

更值得记的是门禁为什么没抓到：`tools/audit-python36-artifact.py` 的检查 4 确实装了扩展、
真的发了一次请求——但那次请求**没有 profile**，`_browser_headers` 在
`if not self.default_headers or not profile: return caller` 处提前返回，
`browser_headers()` 根本没被调用。门禁覆盖了「能发请求」，没覆盖「能发带 profile 的请求」，
而后者才是唯一调用新方法的路径。

同一个 0.2.16 上还有第二个缺陷，同一个原因（类型在边界处没有归一）：
`session.headers = {...}` 把 dict 原样存下，五行之后
`_strip_core_overridden_headers` 调 `Headers.multi_items()` 抛
`'dict' object has no attribute 'multi_items'`。requests 允许这么赋值，所以这不是用户的错。

修法三条，各自堵一类：

1. `Session.headers` 变成 property，setter 一律 `Headers(value)`——一个类型进管道。
2. `platform_identity` 与 `capabilities` 补进 legacy crate（同一个 `minicronet` 依赖，
   几行），并真的跑起来验过：ABI 14、`('X11; Linux x86_64', '"Linux"')`。
3. 新门禁 `tools/audit-binding-surface.py`：读 facade 里每一处 `PyEngine.<name>`，
   读两个 crate 的 `impl PyEngine`，少一个就红。变异测试：删掉 legacy 的那个方法 →
   `legacy (chrome_client_native36) does not declare platform_identity`，退出 1。

顺带把 artifact 门禁的请求改成带 profile，并在扩展缺方法时给出**真实**原因（是扩展没有这个
方法，不是 Core 缺 ABI），因为下一次读日志的人需要的是这个区分。

**判据**：共享同一个 Python 包的两份原生实现，任何一面新增 API，另一面就是要一起改的地方；
只跑一遍「能发一次请求」的门禁证明不了这一点，得去读两处源码。

### 90.1 同一条判据，后来又在同一个版本里抓出两处

写完全量 suite 之后，我把两个扩展**运行时**的属性面打出来对了一遍（不读源码，直接
dir() 类），差集当场给出了第二个缺陷：

    PyResponse  modern-only: ['header_fields', 'http_version', 'reason_phrase']

legacy 的 `PyResponse` 只是个 status/headers/content/ech_status 的壳：那三个字段它
根本没有，`http_version`/`reason_phrase`/`header_fields` 的三段实现被放在了
`impl PyRequest` 里。facade 用 getattr(head, ..., None) 读，于是每一条响应在 cp36
轮子上都是 Response.http_version = None、reason 从状态行反推、header_fields 重新解析——
**静默降级**，不是崩溃，所以没人报过。

同一轮还发现异常类型不一致：`set_cookie(same_site="sideways")` 在 legacy 抛
RuntimeError、在现代扩展抛 ValueError。原因是 legacy 只有一个 `error()`（映射
PyRuntimeError），而现代扩展对**参数**用 PyValueError。用户写 `except ValueError`
在一个轮子上有效、另一个无效。

以及那条被判错的测试：`test_both_names_resolve_to_a_loaded_module` 断言
`MODULE_NAME in sys.modules`。`MODULE_NAME` 是"这个解释器**优先**要的名字"，
不是它一定拿到的名字——cp36 轮子装在 3.9+ 上时 `load()` 会**故意**回退到 legacy，
于是这个测试在一个受支持的配置下失败。它断言的是实现细节，不是契约。

修完之后我把判据从"读源码"升级成"读两处源码"：`tools/audit-binding-surface.py` 现在
同时比对 facade 用到的 `PyEngine` 方法、以及每个 `#[pyclass]` 在两边暴露的属性集合
（`#[pyo3(get)]` 字段 + `#[getter]` 方法）。变异测试：删掉 legacy 的 `#[getter]`，
它报 `PyResponse.attributes differs` 并列出两边集合。

还有一处**门禁覆盖漏洞**：`bindings/python36` 是独立 workspace，根目录的
`cargo fmt --all` 看不见它——实测它的 `rustfmt --check` 在 HEAD 上就是红的，
而没有任何步骤检查过。格式不追（那是全文件重排），但把 `cargo clippy` 加进 CI 和
AGENTS §5：它今天干净，以后只在这个 crate 出现的警告至少有人看得见。

**判据（补一句）**：共享 facade 的两份原生实现，"能跑一个请求"不足以证明一致；要证明的
是**两边的名字集合相同**，而且要按类比，不是按名字比。

