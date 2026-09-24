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

**门禁**：回归测试覆盖 `allow_redirects=False` 与 `max_redirects=5` 两种触发条件，
同步/异步各测一次。

---

## 4. 文档承诺必须与代码一致（fail-closed 是自己的招牌）

**现象**：`discard_cookies=True` 的文档写着 "Neither send nor record cookies"，
实测请求仍然带着 `sid=ABC`。

**根因**：facade 只删了 `Cookie` 头，但 Core 的 Chromium `CookieMonster` 才是权威，
它会重新附加自己存的 cookie。`discard` 只影响了 Python 侧。

**规则**：本项目对外的核心承诺是「无法忠实实现就显式报错，而不是静默忽略」。
凡是被这个承诺覆盖的选项，必须在**两个存储/两层实现**上都生效，否则报错。

**门禁**：`discard_cookies` 现在切换到一个空 cookie store 的同配置 Engine；
回归测试断言 store 里的 cookie 也不得发出。

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
Core；而 `profiles/chrome-153/captures.json` 记录的抓包是
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



