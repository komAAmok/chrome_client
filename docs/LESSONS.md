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

**答案的四个依据**（都可复核）：

| 问题 | 结论 | 决定性依据 |
| --- | --- | --- |
| py36 支持 3.4/3.5 | **不可能** | `pyo3-build-config-0.15.2/src/impl_.rs:21` `MINIMUM_SUPPORTED_VERSION = 3.6`；pyo3 0.15.1 的 feature 表里根本没有 `abi3-py35`/`abi3-py34` |
| 主分支支持 3.14 | **装不上** | 两个 `pyproject.toml` 都写 `requires-python = ">=3.6,<3.14"`；pip 直接拒绝 |
| 主分支支持 3.15 | **装不上** | 同上；且 `ABI3_MAX_MINOR = 14`（`impl_.rs:43`），abi3 的版本上限就是 3.14 |
| 3.14/3.15 free-threaded | **不承诺，且 abi3 在 free-threaded 上不生效** | `impl_.rs:193` `if self.abi3 && !self.is_free_threaded()`；`impl_.rs:730` 明确写「free-threaded build 不支持 abi3」 |

**容易说错的地方**：

1. **abi3 与 free-threaded 是互斥的**。`abi3-py37` 在 3.13t/3.14t 上不会生成 limited API，
   所以「一个 abi3 wheel 顺带覆盖 free-threaded」是错的——必须为 `3.14t` 单独出 wheel，
   且 hint 是 `cp314t` 而不是 `cp314`。
2. **`ABI3_MAX_MINOR` 是硬上限**。pyo3 0.28.3 的上限是 14；跑到 3.15 解释器上它只会
   `warn!` 然后回落（`impl_.rs:751`），不是「自动支持 3.15」。
3. **模块声明了 `gil_used = true`**（`bindings/python/src/lib.rs:846`）。即使装了
   free-threaded 解释器，这个扩展也会被强制重新启用 GIL——「能导入」不等于「并行」。
   代码注释写的是「在专门的 3.13t 测试矩阵出现前保留 GIL」，这是一条有意的选择，不是疏漏。

**规则**：回答「支持到哪个版本」时，必须区分三种状态：**装不上**（元数据拒绝）、
**能装但没测**（风险最高，本仓库 3.6 就长期处在这里）、**测过**。三者不能混说成
「支持」或「不支持」。

**门禁**：`tools/audit-pypi-metadata.sh` 校验 `requires-python`、classifiers 与 README
徽章三者一致；`docs/PYTHON_BINDING_AUDIT.md` 把「free-threaded 3.13t 明确不承诺」
写进表格而不是留给读者猜。
