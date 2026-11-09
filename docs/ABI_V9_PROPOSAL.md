# ABI v9 提案：cookie 存取、协商协议、client hints 注入、DNS 配置

本文件是四项改动进 ABI v9 的提案，不改变 v8 的任何行为。v8 仍是冻结的事实，
把它当成"已发布的契约"读——本文件只描述 v9 会新增什么、为什么新增、以及如何验证。

四项按依赖顺序排列。第 1 项（cookie）是唯一能消除现有补偿逻辑的一项，
第 2 项（协商协议）是唯一在 v8 上**无替代实现**的一项，
第 3、4 项是新增能力而非修复，排在最后。

写作依据：本机 Chromium 检出（`$CHROMIUM_SRC`，见 `tools/core-paths.sh`），条目一律标 `文件:行`。
本机不具备的条件（9 个平台的重建、真实 Chrome 抓包）明确标为"待验证"，不写成本文结论。

---

## 0. 为什么要做 v9

现状里有四处"绑定层替 Core 猜"，每一处都能在代码里指出具体行，并且都有实测数字：

| 症状 | 现有补偿 | 代价（实测） |
| --- | --- | --- |
| 看不到 CookieMonster | `_resolve_cookies` 比较 jar 与镜像，不一致就 `generation += 1` 换一个空 store 的引擎 | 每次调用方 jar 编辑建一个新 Engine：20 次编辑 → `builds=20 generation=19` |
| 看不到协商协议 | `http_version` 恒 `None` | 无法报告 h2/h3 |
| client hints 由 facade 拼 | 不经过 `net` 的 URLLoader 工厂 | `Accept-CH` 协商不生效 |
| 无 DNS 配置 | `doh_url` 主动抛 `UnsupportedFeature` | 无 DoH / 无自定义 nameserver |

第 1 行的实测是本提案最强的动机，而且我上一轮口头说的"服务端每次 Set-Cookie 都触发生成
+1"**是错的**，这里按实测更正：

```
A 只回 Set-Cookie（20 次请求）        builds=1  generation=0    evictions=0
B 每次请求前改一次 jar（20 次）       builds=20 generation=19   evictions=0
C 每次请求后删掉服务端 cookie（10 轮） builds=11 generation=10   evictions=0
```

即触发条件是**调用方写 jar**，不是服务端 Set-Cookie。B 与 C 说明：
只要调用方在一段时间内每轮都动 jar，Engine 就一次都不复用——一条连接都没留下来。

---

## 1. Cookie 存取（`mn_engine_cookies_*`）

### 1.1 Chromium 侧的注入点（已核实）

| 用途 | 接口 | 位置 |
| --- | --- | --- |
| 取 store | `URLRequestContext::cookie_store()` | `net/url_request/url_request_context.h:170` |
| 全量读 | `CookieStore::GetAllCookiesAsync(GetAllCookiesCallback)` | `net/cookies/cookie_store.h:105` |
| 按 URL 读 | `CookieStore::GetCookieListWithOptionsAsync(url, options, partition, cb)` | `net/cookies/cookie_store.h:96` |
| 写入 | `CookieStore::SetCanonicalCookieAsync(cookie, source_url, options, cb, access_result)` | `net/cookies/cookie_store.h:72` |
| 删除单个 | `CookieStore::DeleteCanonicalCookieAsync(cookie, cb)` | `net/cookies/cookie_store.h:122` |
| 清空 | `CookieStore::DeleteAllAsync(cb)` | `net/cookies/cookie_store.h:148` |
| 构造 cookie | `CanonicalCookie::CreateSanitizedCookie(url, name, value, domain, path, ...)` | `net/cookies/canonical_cookie.h:195` |
| 解析 Set-Cookie 行 | `CanonicalCookie::Create(url, cookie_line, creation_time, server_time, partition_key, source_type, status)` | `net/cookies/canonical_cookie.h:178` |
| 读字段 | `CookieBase::Name()`/`Domain()`/`Path()`；`CanonicalCookie::Value()`/`ExpiryDate()`/`IsPersistent()` | `net/cookies/cookie_base.h:83-89`、`canonical_cookie.h:284-288` |

全部是 public 的 `CookieStore` 虚接口，不需要 patch Chromium。回调都在
`CookieMonster` 自己的序列上执行，因此 Core 侧必须把它 `PostTask` 回 engine 的
`task_runner`，不能在回调里直接摸 `mn_` 句柄。

### 1.2 ABI 设计

四对"指针 + 长度"沿用现有结构体风格；容器由调用方提供、由 Core 填充，
容量不足时返回 `MN_ERROR_INVALID_ARGUMENT` 并写出所需数量——与
`mn_request_create` 的空 out-param 约定一致。

```c
/* 一条 cookie 的只读视图。字符串不归调用方释放，有效期到回调返回为止。 */
typedef struct mn_cookie {
  uint32_t size;
  uint32_t version;
  const char *name;
  size_t name_length;
  const char *value;
  size_t value_length;
  const char *domain;
  size_t domain_length;
  const char *path;
  size_t path_length;
  int64_t expiry_unix_seconds;   /* 0 = 会话 cookie */
  uint8_t secure;
  uint8_t http_only;
  uint8_t same_site;             /* mn_cookie_same_site_t */
  uint8_t reserved;
} mn_cookie_t;

typedef enum mn_cookie_same_site {
  MN_COOKIE_SAME_SITE_UNSPECIFIED = 0,
  MN_COOKIE_SAME_SITE_NO_RESTRICTION = 1,
  MN_COOKIE_SAME_SITE_LAX = 2,
  MN_COOKIE_SAME_SITE_STRICT = 3,
} mn_cookie_same_site_t;

/* 读：url 为 NULL 时返回 store 里的全部 cookie（对应 GetAllCookiesAsync），
 * 否则返回该 URL 会携带的 cookie（对应 GetCookieListWithOptionsAsync）。 */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_get(mn_engine_t *engine, const char *url, size_t url_length,
                      mn_cookie_t *out_cookies, size_t capacity,
                      size_t *out_count);

/* 写一条。source_url 决定 domain 校验与 origin-bound 语义。
 * 走 CreateSanitizedCookie + SetCanonicalCookieAsync，与服务器 Set-Cookie 同一路径。 */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_set(mn_engine_t *engine,
                      const mn_cookie_t *cookie,
                      const char *source_url, size_t source_url_length,
                      int64_t creation_unix_seconds);

/* 删一条（三元组定位）。 */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_delete(mn_engine_t *engine,
                         const char *domain, size_t domain_length,
                         const char *path, size_t path_length,
                         const char *name, size_t name_length);

/* 清空整个 store。 */
MN_EXPORT mn_result_t MN_CALL
mn_engine_cookies_clear(mn_engine_t *engine);
```

四个符号。加上第 2 节的 1 个 getter，v8 的 20 个 → v9 的 25 个。

### 1.3 同步还是异步

上面四个必须是**阻塞**的：facade 需要"写完之后下一次请求一定用新值"，异步会让
调用方拿到一个无法断言的句柄。实现上在 Core 内 `base::WaitableEvent` 等待
`CookieMonster` 的回调，并对 `mn_request_start` 已进入飞行状态的请求加锁序约束
（engine 级的 `cookie_mutex_`，`Request::StartOnNetworkThread` 取、这四个函数也取），
避免"读到一半 store 变了"。

拒绝这样做的情况：如果某个调用点无法在 GIL 已释放的路径上调用（PyO3 的
`py.detach`），则改为在绑定层排队。这一点在实现时必须逐调用点核对，不能假设。

### 1.4 绑定层要删掉什么

这是本项的价值所在，删掉的东西比加上的多：

- `sessions.py` `_resolve_cookies` 的整个 read-compare-replace 分支
- `EngineSlot.cookie_header` 镜像与 `_cookie_lock` 里的 generation 推进
- `engine.py` `EngineConfig.generation` 字段（连同缓存键里的那一维）
- `_open_websocket` 里本轮刚加的"分歧就换引擎"分支
- `discard_cookies` 的 `_one_shot_slot` 临时槽（改为 set → 请求 → 恢复，或干脆
  在 Core 侧给请求级开关，见 1.6）

---

## 2. 协商协议（`mn_request_negotiated_protocol`）

### 2.1 为什么 v8 做不到

v8 的 `on_response` 只带 `(status_code, headers)`（`core/abi/minicronet.h:205-209`），
而 Chromium 把 h2/h3 响应规范化成 `HTTP/1.1 200` 的状态行，所以协议只能靠猜。
当前绑定层选择返回 `None`——按 §7 这是正确的，错报比缺失更糟。

### 2.2 Chromium 侧真值（已核实）

```
URLRequest::response_info()                    net/url_request/url_request.h:625
HttpResponseInfo::connection_info              net/http/http_response_info.h:161
HttpConnectionInfo 枚举                        net/http/http_connection_info.h:20-66
HttpConnectionInfoToCoarse()                   net/http/http_connection_info.cc:127
```

`connection_info` 的取值里 `kHTTP1_1` / `kHTTP2` / `kQUIC_*` 足以区分三种协议；
`HttpConnectionInfoToCoarse` 把 `kDEPRECATED_SPDY*` 与 `kDEPRECATED_HTTP2_1*` 也
归到 `kHTTP2`，把全部 `kQUIC_*` 归到 `kQUIC`——**直接用它**，不要自己写 switch，
否则会把 `kQUIC_2_DRAFT_8` 之类漏掉。

### 2.3 ABI 设计

新增一个 getter，而不是扩展 `mn_request_callbacks_t`。理由：扩展回调用途更窄
（只在 header 阶段有效），而 getter 可以在完成后、甚至 `mn_request_cancel` 之后调用，
且不改变任何结构体布局——**这意味着 `mn_request_callbacks_t` 仍是 48 字节，
`mn_request_config_t` 仍是 144 字节，32 位分支的 28/92 也不用重算**。

```c
typedef enum mn_http_version {
  MN_HTTP_VERSION_UNKNOWN = 0,
  MN_HTTP_VERSION_0_9 = 1,
  MN_HTTP_VERSION_1_0 = 2,
  MN_HTTP_VERSION_1_1 = 3,
  MN_HTTP_VERSION_2 = 4,
  MN_HTTP_VERSION_3 = 5,
} mn_http_version_t;

/* 必须在 on_response 或 on_complete 之后调用；此前返回 MN_ERROR_INVALID_STATE。
 * 返回 MN_OK 且 *out_version == MN_HTTP_VERSION_UNKNOWN 表示 Chromium 自己也不知道
 * （例如被缓存的响应），调用方应继续报告 None。 */
MN_EXPORT mn_result_t MN_CALL
mn_request_negotiated_protocol(mn_request_t *request,
                               mn_http_version_t *out_version);
```

`UNKNOWN` 单独存在是刻意的：`connection_info` 的默认值就是 `kUNKNOWN`
（`http_response_info.h:161`），把它报成 `HTTP/1.1` 就是本提案要根除的那类谎报。

### 2.4 绑定层

`Response.http_version` 从 `None` 变为真值；对 `MN_HTTP_VERSION_UNKNOWN` 仍返回
`None`。文档 `docs/COMPATIBILITY_BOUNDARY.md` 的"ABI cannot provide"那一段随之改写。

---

## 3. Client hints 由 Core 注入

**这一项不需要新 ABI 字段。** 它需要的是数据，不是接口。

注入点已在 v8 可用：`Request::StartOnNetworkThread` 里的 `SetExtraRequestHeaders`
（`core/source/request.cc`）。现在缺的是每个 profile 的 `UserAgentMetadata`
（品牌列表及 GREASE 顺序、full version、platform、mobile 标志）。

### 3.1 为什么放 v9 而不是立刻做

因为按 AGENTS.md §9，client hint 的**取值必须逐版本从官方源码取**，不能从相邻版本继承。
72 个 profile × 每个的 `UserAgentMetadata` 是一份新的证据工程，和 ABI 改动正交。
把它挂在 v9 一起发，会把"ABI 契约变更"和"72 份新证据"两个失败面混在一起。

本提案的建议：v9 只留出**注入的开关与数据入口**，即
`mn_engine_config_t` 增加一个 `inject_client_hints`（`uint8_t`，0/1），
默认 0；profile 数据仍由 facade 通过 `SetExtraRequestHeaders` 提供。
等数据齐了，再把开关置 1 并让 Core 自己序列化。这样 v9 不阻塞在 72 份证据上。

字段追加在结构体末尾，`size`/`version` 校验会自然拒绝旧调用方。

### 3.2 验收标准（沿用 NEXT_STEPS 已写的那条）

`impersonate="chrome_155"` 时请求头集合与真实 Chrome 155 抓包一致（含顺序），
`impersonate=None` 时**不**注入任何 client hint。第二条是防回归的重点：
默认路径不能因为补 profile 而被污染。

---

## 4. DNS 配置

### 4.1 注入点（已核实，沿用 NEXT_STEPS:254 的记录）

`net/dns/public/dns_config_overrides.h` 的 `DnsConfigOverrides`（nameservers /
secure_dns_mode / dns_over_https_config / DoT / ndots / attempts / rotate …），
注入路径 `HostResolverManager::SetDnsConfigOverrides()`
（`net/dns/host_resolver_manager.h:201`）或
`HostResolver::ManagerOptions::dns_config_overrides`（`host_resolver_manager.cc:495`）。
`net/BUILD.gn:97` 定义 `ENABLE_BUILT_IN_DNS`，保证 overrides 真正生效而不是空转。

### 4.2 ABI 设计

`mn_engine_config_t` 末尾追加（指针 + 长度成对，与现有风格一致）：

```c
  /* 逗号分隔的 nameserver 列表（"8.8.8.8:53,1.1.1.1"）。空 = 用系统配置。 */
  const char *dns_nameservers;
  size_t dns_nameservers_length;
  /* mdns/doh 模式：0=off 1=automatic 2=secure。 */
  uint8_t dns_secure_mode;
  uint8_t dns_over_tls;          /* 0/1 */
  uint8_t dns_rotate;
  uint8_t dns_padding;
  /* DoH 模板（"https://dns.example/dns-query"）。空 = 不用 DoH。 */
  const char *doh_template;
  size_t doh_template_length;
```

绑定层随之解除 `doh_url` 的 fail-closed 分支（`sessions.py:1058` 与 `:1834` 两处），
`EngineConfig` 加字段并进缓存键。

### 4.3 它是新功能，不是修复

v8 上没有"DNS 行为不对"的缺陷——DNS 走 Chromium 内部默认值，与真实 Chrome 一致。
所以本项排最后：**没有任何现有缺陷因它而关闭**。

---

## 5. 发布必须同步的载体（9 个平台）

ABI 版本从 8 到 9 不是一个数字的改动。以下每一处都必须同一次提交内改完，
否则 `tools/audit-abi.sh` 会失败——这是设计意图，不是障碍。

### 5.1 源头与声明

| 载体 | 改什么 |
| --- | --- |
| `core/abi/minicronet.h` | `MN_ABI_VERSION` 8u → 9u；新增 5 个符号声明、`mn_cookie_t` / 两个枚举 |
| `crates/minicronet-sys/src/lib.rs` | 5 个 `pub fn`；`#[repr(C)]` 新结构；`abi_layout_tests` 增补 |
| `core/source/minicronet.cc` | 5 个 `MN_CALL` 定义 |
| `core/source/engine.cc` / `request.cc` | cookie 转发与 `connection_info` 读取 |
| `tools/audit-abi.sh` | `symbols=(` 数组加 5 项；版本断言 8u → 9u；输出文案里的 "ABI v8" |

### 5.2 导出表（三份）

| 文件 | 改什么 |
| --- | --- |
| `core/exports/minicronet.lds` | `MINICRONET_2 { ` → `MINICRONET_3 {`，加 5 个符号 |
| `core/exports/minicronet.def` | `EXPORTS` 段加 5 个符号 |
| `core/exports/minicronet.exports` | 加 5 个符号 |

### 5.3 九个平台产物与 manifest

`core/binaries/targets.json` 的 `abi_version` 改 9，然后**九个目录全部重建**，
每个 `manifest.json` 的 `abi_version` 改 9、`sha256` 与 `size_bytes` 按新产物重写：

| # | 目录 | triple | 产物 |
| --- | --- | --- | --- |
| 1 | `linux-x86` | `i686-unknown-linux-gnu` | `libminicronet.so` |
| 2 | `linux-x86_64` | `x86_64-unknown-linux-gnu` | `libminicronet.so` |
| 3 | `linux-armv7` | `armv7-unknown-linux-gnueabihf` | `libminicronet.so` |
| 4 | `linux-arm64` | `aarch64-unknown-linux-gnu` | `libminicronet.so` |
| 5 | `windows-x86` | `i686-pc-windows-msvc` | `minicronet.dll` |
| 6 | `windows-x86_64` | `x86_64-pc-windows-msvc` | `minicronet.dll` |
| 7 | `windows-arm64` | `aarch64-pc-windows-msvc` | `minicronet.dll` |
| 8 | `macos-x86_64` | `x86_64-apple-darwin` | `libminicronet.dylib` |
| 9 | `macos-arm64` | `aarch64-apple-darwin` | `libminicronet.dylib` |

当前 v8 的九个 manifest 全部记录 `abi_version: 8`，sha256 分别为
`a33178b73092`(linux-x86_64) / `61333f1c60de`(linux-x86) / `6ef28388d2d5`(linux-armv7) /
`a5fa6c39dd3a`(linux-arm64) / `5eadac0501f4`(windows-x86) / `e27e118beb39`(windows-x86_64) /
`8696deafdab6`(windows-arm64) / `c6c5f2b49f6e`(macos-x86_64) / `7f0552f946d7`(macos-arm64)。
v9 会全部改变，这是判据：**只要有一个 manifest 的 sha256 没变，就说明那个平台没重建。**

### 5.4 布局断言

`sizes_and_enum_values_match_public_header`（`crates/minicronet-sys/src/lib.rs:791`，断言在 792-815）
的两套断言都要跟着改：

- 64 位：`mn_engine_config_t` 152（DNS 追加 2 指针 + 4 字节标志，按 8 字节对齐
  需重算）、`mn_request_config_t` 144（不变）、`mn_cookie_t` 需新增
- 32 位：`mn_engine_config_t` 84、`mn_request_config_t` 92（不变）、`mn_cookie_t` 需新增

**注意**：32 位分支目前没有 CI 执行路径（见 `NEXT_STEPS.md`「32 位 ABI 布局没有执行路径」）。
这不是本提案能解决的，但本提案会让那个分支再增两处需要手算的断言——
建议在同一次 v9 里把该分支接上 `cargo test --target i686-unknown-linux-gnu`（可跑）。

### 5.5 文档

`docs/ABI.md`（新增 v9 章节）、`docs/COMPATIBILITY_BOUNDARY.md`（`http_version`、
cookie 双 store 两节作废）、`docs/NEXT_STEPS.md`（把 2 节标为已完成）、
`README.md` / `README.en.md` / `bindings/python36/README.md`（能力表）、
`AGENTS.md:36`（"当前 ABI v8，**20** 个导出符号" → v9，24 个）。
`docs/review-<version>/` 的 JSON 重新生成。

---

## 6. 三条验收用例

每条都要求"在 v8 上失败、在 v9 上通过"，否则不能证明 v9 解决了什么。

### 用例 1：调用方 jar 编辑不再重建 Engine

```python
# 前置：服务端每响应都 Set-Cookie；调用方每轮改写一个同名 cookie
with chrome_client.Session() as s:
    for i in range(20):
        s.cookies.set("sid", str(i), domain="127.0.0.1", path="/")
        s.get(base + "/x")
    assert s.stats["builds"] == 1, s.stats      # v8 实测 20
    assert s.stats["generation"] == 0, s.stats  # v8 实测 19
```

配套断言（证明 cookie 真的写进了 Core，而不是 facade 自己记着）：

```python
with chrome_client.Session() as s:
    s.cookies.set("a", "1", domain="127.0.0.1", path="/")
    s.get(base + "/echo-cookie")   # 服务端回显 Cookie 头
    assert "a=1" in last_request_cookie
    # 绕过 facade 直接问 store：证明不是 facade 拼的头
    assert any(c.name == "a" and c.value == "1"
               for c in s._engine_cookies())          # v9 新增入口
    s._engine_cookies_delete("127.0.0.1", "/", "a")
    assert not any(c.name == "a" for c in s._engine_cookies())
    s.get(base + "/echo-cookie")
    assert "a=1" not in last_request_cookie
```

### 用例 2：`http_version` 报告真值

```python
# 同一台 h2 端点，v8 下 http_version 恒 None
for pin, expected in ((None, "HTTP/2"), ("v1", "HTTP/1.1"), ("v3", "HTTP/3")):
    r = chrome_client.get(https_url, http_version=pin)
    assert r.http_version == expected, (pin, r.http_version)

# 语义边界：Chromium 自己不知道时必须仍是 None，不能谎报 HTTP/1.1
r = cached_get(https_url)             # 命中 HTTP 缓存
assert r.http_version is None          # connection_info == kUNKNOWN
```

### 用例 3：Cookie store 与服务器 Set-Cookie 同一条路径

证明 `mn_engine_cookies_set` 走的是 `CreateSanitizedCookie` + `SetCanonicalCookieAsync`，
而不是把值塞进某个旁路：

```python
with chrome_client.Session() as s:
    # 1) 非法 domain 必须被 store 拒绝，而不是被 facade 拒绝
    with pytest.raises(chrome_client.CookieRejected):      # 新增异常
        s._engine_cookies_set(name="a", value="1",
                              domain="other.invalid",
                              source_url="http://example.invalid/")
    # 2) 合法写入后，服务器看到的与 store 里的完全一致
    s._engine_cookies_set(name="a", value="1", domain="example.invalid",
                          path="/", source_url="http://example.invalid/")
    s.get("http://example.invalid/echo-cookie")
    store = {(c.domain, c.path, c.name): c.value for c in s._engine_cookies()}
    assert store[("example.invalid", "/", "a")] == "1"
    assert last_request_cookie == "a=1"
    # 3) HttpOnly cookie 从 Core 读回来不再丢失该标志（v8 的 jar 镜像会丢）
    s._engine_cookies_set(name="h", value="1", domain="example.invalid",
                          path="/", http_only=True,
                          source_url="http://example.invalid/")
    assert any(c.name == "h" and c.http_only for c in s._engine_cookies())
```

第 3 点尤其重要：v8 的 facade 只镜像最终响应与各跳的 `Set-Cookie`，**没有**从 store
读回的能力，因此任何由 Core 内部产生（重定向中间跳、未回显的 3xx）或带 `HttpOnly` 的
cookie，镜像都可能缺字段。这是本提案第 1 项真正的收益，比"少建几个 Engine"更重要。

---

## 7. 顺序与风险

建议的落地顺序，按"能否独立验证"排：

1. **第 2 项（协商协议）先做**——只增一个 getter、不改任何结构体布局，
   32 位断言不用动，风险最低，且它是唯一无替代实现的一项。
2. **第 1 项（cookie）第二**——改动最大（删绑定层一整套补偿逻辑），
   但每删一处都有实测数字作对照，回滚边界清楚。
3. **第 4 项（DNS）第三**——纯新增字段。
4. **第 3 项（client hints）最后**——阻塞在 72 份 profile 证据上，不阻塞 v9 发布。

风险清单：

- **第 1 项的锁序**：cookie 函数与 `Request::StartOnNetworkThread` 共享 engine 状态，
  锁序写错会在 Chromium 网络线程上死锁。必须先写一个只在两线程间交替调用的
  `--gtest_repeat` 压测再合。
- **`mn_cookie_t` 的指针有效期**：与 v8 现有回调约定一致（只在回调期间有效），
  但 `cookies_get` 是同步返回、填充调用方 buffer，语义不同，必须在头文件注释里
  写清楚是"填充 caller 提供的数组"而不是"借用指针"。
- **`kUNKNOWN` 的诚实性**：如果实现时图省事把 `kUNKNOWN` 报成 `HTTP/1.1`，
  本提案就白做了。用例 2 的第二段是专门的守卫。

---

## 8. 与现有 v8 缺陷的关系

本提案**不关闭**上一轮 review 里剩下的三项 Major：

| 残留 | 是否因 v9 关闭 |
| --- | --- |
| `headers={"User-Agent": None}` 无法删除 Core 注入的头 | **否** —— 需要的是"删除"语义，不是 cookie/协议/DNS；可在 v9 顺带加一位开关，但不是本提案范围 |
| `tools/audit-python-stubs.py` 硬编码 `AUDITED` | **否** —— 纯 Python 门禁 |
| `bindings/python36` 门禁空壳与 `include` 路径 | **否** —— 构建配置 |

也就是说，把 v9 做完，仓库里仍然有这三项待办。它们不应等 v9，应该单独修。
