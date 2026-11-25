# ABI v9 提案：cookie 存取、协商协议、client hints、DNS、host mapping、mTLS、原生能力补齐、平台身份、非幂等重发、hop-by-hop 头

本文件是十项改动进 ABI v9 的提案，不改变 v8 的任何行为。v8 仍是冻结的事实，
把它当成"已发布的契约"读——本文件只描述 v9 会新增什么、为什么新增、以及如何验证。

十项按依赖顺序排列。第 1 项（cookie）是唯一能消除现有补偿逻辑的一项，
第 2 项（协商协议）是唯一在 v8 上**无替代实现**的一项，
第 3、4、5、6 项是新增能力。其中两项虽是新能力，却能当场关闭现存问题：第 5 项
（host mapping）让 `ERR_NAME_NOT_RESOLVED` 从"本机不可测"变为可测，第 6 项（mTLS）
让一整类双向认证部署从"完全不可用"变为可用。第 4 项（DNS）则是纯新功能。

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
| 无法分离连接目标与 SNI | 无；`interface=` 被拒 | CDN 直连不可表达，`ERR_NAME_NOT_RESOLVED` 在本机不可测 |
| 双向认证不可用 | `cert=` 主动抛 `UnsupportedFeature` | 企业内网 / 金融网关 / IoT 管理这一整类部署无法使用 |
| ECH 结果不可见 | ABI v8 回调只有 `(status, headers)` | 分不清"服务器不支持"与"平台已关闭" |
| HSTS 未启用 | `core/source/engine.cc` 零命中 hsts | 对 HSTS 站点先发明文 `http://`，Chrome 不会 |
| Cookie 语义非 Chrome 默认 | 无 `CookieAccessDelegate` | 同一份 `Set-Cookie` 可能落进不同 SameSite 桶 |
| H2 session params 用 C++ 默认 | 只设了 `protocol_mode` / `ignore_certificate_errors` | END_STREAM 帧选择、SETTINGS GREASE 与 Chrome 可能不同 |
| 传参 UA 与 client hints 可互相矛盾 | UA 与 hint 各自独立决定，无约束 | Linux 上发 Windows UA 而 platform hint 消失或仍是 Linux |
| 默认 UA 是库名 | `minicronet.cc:48` `kDefaultUserAgent = "minicronet/" MINICRONET_VERSION` | `impersonate=None` 时 UA 为 `minicronet/0.2.9`，一眼看出不是浏览器 |
| 非幂等请求被静默重发 | `http_network_transaction.cc:2388-2396` 判据不看幂等性 | `retry=0` 下 POST 仍被发两次，body 也重发 |
| `Connection` 等 hop-by-hop 头能发出但不生效 | `SetExtraRequestHeaders` 不参与连接池；h2/h3 上被静默丢弃（`spdy_http_utils.cc:214-221`） | `Connection: close` 不关闭连接，且行为随协商协议变化 |

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

四个符号。加上第 2 节的 2 个 getter（协商协议 + ECH 状态）与第 6 节的 2 个 setter
（mTLS），v8 的 20 个 → v9 的 **28 个**。第 3、4、5 项只追加 `mn_engine_config_t`
字段，不新增导出符号。

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

### 2.5 ECH：只暴露状态，不暴露配置

ECH 值得单列，因为"能不能配"和"能不能看"是两个完全不同的问题，而本项目只该做后者。

**配置这一侧在桌面端根本不存在。** `SSLConfig::ech_config_list`（`net/ssl/ssl_config.h:138`）
确实是传进来的，但唯一的生产者是 DNS 的 HTTPS 记录：

```c
ssl_connect_job.cc:404-412
  if (IsEchEnabled(ssl_client_context(), params_->host_and_port())) {
    if (ech_retry_configs_)      ssl_config.ech_config_list = *ech_retry_configs_;
    else if (endpoint_result_)   ssl_config.ech_config_list = endpoint_result_->metadata.ech_config_list;
    ...
  }
```

开关也不在调用方手里。`IsEchEnabled` 读的是 `GetEchMode`（`ssl_client_context.cc:255`），
而 `GetEchMode` 是 `ssl_config_service.h:169` 的纯虚接口；
`url_request_context_builder.cc:392-395` **只在 `BUILDFLAG(IS_ANDROID)` 下**装
`EchModeGetterAndroid`，其余平台 `ech_mode_getter` 是 nullptr。也就是说：

**Linux / macOS / Windows 上 `EchMode` 恒为 `kDisabled`，ECH 不会发出**，
无论 DNS 里有没有 HTTPS 记录。这与客户端库无关，是 Chromium 桌面端的现状。

**观测这一侧是真实缺口。** `net/ssl/ssl_info.h` 记了本次连接的 ECH 结果，
而 ABI v8 的 response 回调只有 `(status, headers)`，看不到它。所以现在的情况是：
ECH 没生效时，调用方无从判断是"服务器不支持"、"DNS 没给记录"还是"平台关掉了"。

```c
typedef enum mn_ech_status {
  MN_ECH_NOT_ATTEMPTED = 0,   /* 平台关闭，或没有 ECHConfigList */
  MN_ECH_SUCCESS = 1,         /* 握手用了 ECH */
  MN_ECH_FAILED = 2,          /* 尝试了但被拒；retry configs 已在 Core 内部处理 */
} mn_ech_status_t;

/* 与 mn_request_negotiated_protocol 同为"完成后可读"，同样在 on_response /
 * on_complete 之前返回 MN_ERROR_INVALID_STATE。 */
MN_EXPORT mn_result_t MN_CALL
mn_request_ech_status(mn_request_t *request, mn_ech_status_t *out_status);
```

加一个 getter，不加任何 config 字段。理由：

1. **不在 Core 里重建 Chrome 从不走的路。** 若暴露 `ech_config_list`，就必须自己实现
   一个 `EchModeGetter` 并把配置塞进 per-connection 的 `SSLConfig`，等于绕开 DNS 路径
   另造一条——那正好是 AGENTS §1 禁止的"在绑定层/Core 里重实现协议语义"。
2. **`NOT_ATTEMPTED` 单独存在是刻意的。** 把"没尝试"报成"失败"会让人去查服务器配置，
   而真实原因是平台关掉了 ECH。这与第 2.3 节 `MN_HTTP_VERSION_UNKNOWN` 是同一条原则。
3. 与协商协议同一形态、同一批落地，风险合并，也不新增结构体所以布局断言不动。

绑定层：`Response.ech_status` 返回 `"not_attempted"` / `"success"` / `"failed"`。

> **修正（实测）**：本提案原文要求文档写清「桌面端恒为 `not_attempted`」，这条是错的。
> 决定 ECH 能否成功的不是平台，而是引擎有没有自己去查 DNS HTTPS 记录：
> `core/source/engine.cc:801` 只在传了任一 dns_* 参数时才建 standalone resolver。
> 实测 chrome_158 对 `crypto.cloudflare.com`：默认 resolver `not_attempted`（sni 明文）；
> `dns_nameservers="1.1.1.1"` 5/5 `success`（sni 加密）。矩阵见 `docs/ABI.md` 末节。
> 第 2 条理由里「真实原因是平台关掉了 ECH」因此也不成立——`GetEchMode` 对 119+ 一律返回 `kOpportunistic`。


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

## 5. Host mapping：把连接目标与 SNI / Host 分开

这一项不在最初的清单里，是评估第 7 项（IP + Domain 模式）时补上的。它比第 4 节的 DNS
配置**优先级更高**：DNS 是纯新功能，不关闭任何缺陷；host mapping 能关闭一个现存测试盲区，
并且是 CDN 直连场景的前置条件。

### 5.1 Chrome 原生支持，且是两条不同的解析路径

Chrome 用 `MappedHostResolver` + `set_host_mapping_rules()` 实现，关键在于**改写只发生在
解析层**，所以连接目标变了而 SNI 没变：

```c
// net/dns/mapped_host_resolver.cc:70-78  -- 连接目标被改写
HostPortPair rewritten = host;
rules_.RewriteHost(&rewritten);
if (rewritten.host() == "^NOTFOUND")
  return CreateFailingRequest(ERR_NAME_NOT_RESOLVED);
return impl_->CreateRequest(rewritten, ...);

// net/socket/ssl_connect_job.cc:446-448  -- SNI 用原 host，不经过 MAP
ssl_socket_ = client_socket_factory()->CreateSSLClientSocket(
    ssl_client_context(), std::move(nested_socket_), params_->host_and_port(),
    ssl_config);

// net/socket/ssl_client_socket_impl.cc:720
SSL_set_tlsext_host_name(ssl_.get(), host_and_port_.host().c_str());
```

四条取值各走各的，这正是 `chrome://net-internals` 里 `MAP` 规则的语义：

| 环节 | 取值来源 | 结果 |
| --- | --- | --- |
| TCP 目的地址 | `MappedHostResolver` 改写后的 host | IP 字面量 |
| TLS SNI | `host_and_port_`（URL host，未改写） | 原域名 |
| `Host:` 请求头 | URL host | 原域名 |
| 证书校验主机名 | `host_and_port_` | 原域名 |

单测已经钉住这点：`net/dns/mapped_host_resolver_unittest.cc:149-166` 用
`MAP host.test [1234:5678::000A]` 请求 `host.test`，得到
`IPEndPoint(1234:5678::000A, 156)`——解析结果是 IP，而调用方给的仍是域名。

**接入点不需要 patch Chromium**：`URLRequestContextBuilder::set_host_mapping_rules()`
（`net/url_request/url_request_context_builder.h:276`）是 public 方法，而
`core/source/engine.cc` 构造 context 时已经在用同一个 builder。命令行等价物是
Chrome 的 `--host-resolver-rules`。

### 5.2 ABI 设计

engine 级，不是 per-request。理由：`set_host_mapping_rules` 是 context 构造参数，
per-request 需要再包一层 `MappedHostResolver` 并重建 connect job 的 host 来源，复杂度
不成比例；而 mapping 规则本身很少变。

```c
/* mn_engine_config_t 末尾追加（指针 + 长度，与现有字段同风格）。
 * 语法即 Chromium 的 MappedHostResolver：
 *   "MAP example.com 93.184.216.34"
 *   "MAP example.com 93.184.216.34:443"
 *   "MAP *.example.com ^NOTFOUND"
 *   "EXCLUDE bypass.example.com"
 * 空字符串 = 不加任何规则，行为与 v8 完全一致。 */
const char *host_mapping_rules;
size_t host_mapping_rules_length;
```

放在 `mn_engine_config_t` 末尾而不是新开一个 setter，是因为 `set_host_mapping_rules`
本身就是 context 构造参数，放进 config 与它的语义一致，也让 `size`/`version` 校验自然
拒绝旧调用方。

### 5.3 绑定层

1. `EngineConfig` 加字段，且**必须进缓存键**——否则两组不同 mapping 会复用同一个
   engine，规则静默失效。
2. `Session(host_mapping=[("example.com", "93.184.216.34")])`，内部拼成
   `MAP example.com 93.184.216.34`；也接受直接传规则字符串，因为 `EXCLUDE` 与
   `^NOTFOUND` 两种形式用 pair 表达不出来。
3. 同时解除 `interface=` 的拒绝：那是个独立的连接绑定能力，与 mapping 无关，
   两者可以并存（先 bind 接口，再按规则选地址）。

**不要做的事**：不要在绑定层自己实现“连 IP、报域名”。那等于把 DNS 与 SNI 的对应关系
重写一遍，是 AGENTS §1 明令禁止的（绑定层不得实现协议语义）。

### 5.4 它关闭的两个现存问题

**其一：CDN 直连无法表达。** 现在只能把 URL 改成 `https://93.184.216.34`，于是 SNI 变成
IP、证书按 IP 校验，必然失败。MAP 是唯一不改 URL 就能分离的机制。

**其二：`ERR_NAME_NOT_RESOLVED` 在本机不可测。** 这台机器的 DNS 把 `.invalid` 解析到
`198.18.0.224`，所以 `DNSError` 这条映射路径一直只能标注“环境限制”。有了 MAP，
`MAP *.invalid ^NOTFOUND` 能**确定性地**制造 `ERR_NAME_NOT_RESOLVED`
（`mapped_host_resolver.cc:76-78` 显式返回失败请求），这条错误映射从此可测。
这不是新功能，是补上一个原本测不了的断言。

### 5.5 与第 4 节 DNS 配置的关系

两者正交，可以分开落地：

| | 改什么 | 影响层 |
| --- | --- | --- |
| host mapping（本节） | 某个域名解析到哪个地址 | `MappedHostResolver`，解析结果替换 |
| DNS 配置（第 4 节） | 用哪些 nameserver、是否 DoH/DoT | `DnsConfigOverrides`，解析方式 |

顺序上建议**host mapping 先做**：单字段、语义已由 Chromium 定义并测试、能立刻关闭一个
测试盲区；DNS 配置涉及 nameserver 列表校验与 DoH 模板解析，失败面更大。

### 5.6 验收用例

```python
# 1. 连接打到 IP，SNI / Host / 证书校验仍用域名
#    服务端起一个 TLS 监听器，证书签给 example.test，只绑 127.0.0.1
with chrome_client.Session(
        host_mapping=[("example.test", "127.0.0.1")],
        verify=ca_bundle_for_example_test) as session:
    response = session.get("https://example.test:%d/" % port)
assert response.status_code == 200
assert server.last_sni == "example.test"      # 不是 127.0.0.1
assert server.last_host_header == "example.test:%d" % port
```

```python
# 2. ^NOTFOUND 确定性地产生 DNSError（本机 DNS 拦截下也能测）
with chrome_client.Session(host_mapping=["MAP *.invalid ^NOTFOUND"]) as session:
    with pytest.raises(chrome_client.DNSError) as caught:
        session.get("http://blocked.invalid/")
assert "-105" in str(caught.value)          # ERR_NAME_NOT_RESOLVED
```

```python
# 3. 规则进缓存键：两组 mapping 必须拿两个不同的 engine
a = chrome_client.Session(host_mapping=[("a.test", "127.0.0.1")])
b = chrome_client.Session(host_mapping=[("b.test", "127.0.0.1")])
a.get(url_a); b.get(url_b)
assert a.stats["builds"] + b.stats["builds"] >= 2   # 不能共用一个 engine
```

第 1 条是核心：它同时验证 SNI、`Host` 头与证书校验三者用的都是域名，而不是 IP。
只断言“连上了”不够——连上恰恰是最容易做到、也最容易做错的那一半。

### 5.7 风险

- **规则语法不在绑定层校验**：语法错误由 Core/Chromium 报，绑定层不得自己写解析器，
  否则又多一份需要与 Chromium 同步的实现。
- **`^NOTFOUND` 与 `EXCLUDE` 的区别必须写进文档**：前者让解析失败，后者让规则不生效。
  两者都能用来“绕过”，混用会得到完全不同的结果。
- **不碰证书校验语义**：MAP 不改校验主机名。想连一台证书签给别的域名的机器，仍然需要
  `verify=False` 或自定义 CA，这是两个独立能力，不要合并成一个参数。

---

## 6. mTLS：客户端证书

这一项和第 5 项一样是评估时补的。它比第 5 项更接近"基础能力"：host mapping 是某些场景
做不了，mTLS 是一整类部署用不了（企业内网 API、金融网关、IoT 设备管理）。

### 6.1 Chromium 原生支持，注入点是 public 的

```c
net/socket/ssl_client_socket.h:216   （public 段：131 起，private 段：268 起）
  void SetClientCertificate(const HostPortPair& server,
                            scoped_refptr<X509Certificate> client_cert,
                            scoped_refptr<SSLPrivateKey> private_key);
net/socket/ssl_client_socket.h:226
  bool ClearClientCertificate(const HostPortPair& server);
```

内部机制已经现成，而且 Chromium 自己处理过一个真实陷阱：

```c
ssl_client_socket.cc:267-271
  GetClientCertificate(server, &cert, &key)
    -> ssl_client_auth_cache_.Lookup(server, cert, key);        // per-server 缓存

ssl_client_socket.cc:274-288  SetClientCertificate(...)
  -> ssl_client_auth_cache_.Add(server, cert, key)
  -> if (ssl_client_session_cache_) FlushForServers({server});  // ← 关键
  -> NotifySSLConfigForServersChanged({server})
```

`FlushForServers` 那行是本项最容易漏掉的地方：**TLS 会话恢复会跳过客户端证书协商**，
所以换证书必须清掉该 server 的会话缓存。Core 不需要自己想到这点——调
`SetClientCertificate` 就自动做对了，但**绑定层换证书时仍必须清掉该 server 的 Engine
缓存**，否则新证书在旧 Engine 上不生效。

密钥与证书的构造路径同样是 public：

```c
net/ssl/openssl_private_key.h:21
  NET_EXPORT scoped_refptr<SSLPrivateKey> WrapOpenSSLPrivateKey(bssl::UniquePtr<EVP_PKEY> key);
net/cert/x509_certificate.h:103
  static scoped_refptr<X509Certificate> CreateFromDERCertChain(...);
```

即 `PEM -> EVP_PKEY -> WrapOpenSSLPrivateKey` 与 `PEM -> DER -> CreateFromDERCertChain`。

### 6.2 为什么它不破坏已冻结的指纹

客户端证书只在服务端发 `CertificateRequest` 之后才发送，不改变 ClientHello 的扩展集合
与顺序（`certificate_authorities` 由 BoringSSL 按需处理）。所以对 72 个已冻结 profile 的
h1/h2/h3 指纹没有影响——**这是少数"加了也不污染指纹"的扩展能力**，与第 3 项
（client hints 必须逐版本取证据）性质完全不同。

### 6.3 ABI 设计

per-server 的 setter，不是 config 字段，也不是全局默认。

```c
/* 登记某个 server 的客户端证书。cert_pem / key_pem 是 PEM 字节串。
 * 与 SetClientCertificate 同语义：按 (host, port) 缓存，只在该 server 请求证书时使用。 */
MN_EXPORT mn_result_t MN_CALL
mn_engine_set_client_certificate(mn_engine_t *engine,
                                 const char *host, size_t host_length,
                                 uint16_t port,
                                 const uint8_t *cert_pem, size_t cert_pem_length,
                                 const uint8_t *key_pem, size_t key_pem_length);

MN_EXPORT mn_result_t MN_CALL
mn_engine_clear_client_certificate(mn_engine_t *engine,
                                   const char *host, size_t host_length,
                                   uint16_t port);
```

为什么是 per-server setter 而不是 config 字段：`SetClientCertificate` 本身就是
per-server 语义；做成全局默认反而要在 Core 里维护一份"默认证书"映射，凭空多一层。
而证书常在 engine 建好之后才拿到（先登录换证书），config 是构造期参数，塞不进去。

两个新符号，v9 的 25 → 27。

### 6.4 绑定层

```python
with chrome_client.Session(
        client_cert=("client.pem", "client.key"),                   # 默认
        client_cert_by_host={("api.internal", 443): ("a.pem", "a.key")},
) as session:
    session.get("https://api.internal/")

# 运行期换证书时必须同时清 Engine 缓存，否则旧 Engine 仍用旧证书
session.set_client_certificate("api.internal", 443, "new.pem", "new.key")
```

同时解除 `_prepare_call` 里对 `cert=` 的拒绝（`sessions.py:1834` 那一行），让 requests
的 `cert=` 语义对上：`cert=("cert.pem", "key.pem")` 或单个合并的 PEM 都接受。

### 6.5 验收用例

``````python
# 1. 服务端要求客户端证书，握手成功且服务端读到正确的 CN
with chrome_client.Session(
        client_cert=("client.pem", "client.key"),
        verify=ca_bundle) as session:
    response = session.get("https://mtls.test:%d/" % port)
assert response.status_code == 200
assert server.peer_certificate_cn == "client.test"
``````

``````python
# 2. 不提供证书时，服务端拒绝握手 -> 映射成连接错误而不是静默成功
with chrome_client.Session(verify=ca_bundle) as session:
    with pytest.raises(chrome_client.ConnectionError):
        session.get("https://mtls.test:%d/" % port)
``````

``````python
# 3. 换证书必须生效（回归 FlushForServers 那条路径）
with chrome_client.Session(client_cert=("a.pem", "a.key"), verify=ca) as s:
    s.get(url)                       # 服务端记到 CN=a.test
    s.set_client_certificate("mtls.test", 443, "b.pem", "b.key")
    s.get(url)                       # 必须是 CN=b.test，不能是恢复的旧会话
assert server.seen_cns[-2:] == ["a.test", "b.test"]
``````

第 3 条是核心：它守的正是 Chromium 自己遇到过的问题——会话恢复会跳过客户端证书协商。
只测"能连上"抓不到它。

### 6.6 风险

| 风险 | 处理 |
| --- | --- |
| 私钥驻留进程内存 | `WrapOpenSSLPrivateKey` 把密钥留在内存，没有 OS keystore 保护。文档必须写明，不得暗示有硬件保护 |
| 会话恢复绕开协商 | 由 `FlushForServers` 处理；绑定层换证书时必须同时丢 Engine |
| 加密私钥（带 passphrase） | **v9 先不支持并 fail-closed**，不要静默失败；解析失败要给出明确错误 |
| 证书/私钥路径泄露 | 异常消息只允许出现 host/port，不打印路径以外的内容 |
| 与 `verify=` 混淆 | 两者独立：`verify` 管服务端证书校验，`client_cert` 管我方出示的证书。不要合并成一个参数 |

---

## 7. 补齐四项「原生已支持但 Core 没用」的能力

这一节来自一次系统审计：把 `URLRequestContextBuilder` 的 **41 个 `set_*` 入口**与 Core
实际使用的 **8 个**做差集，再剔掉六项与本项目无关的。剔除理由逐项给出依据，因为
「无意义」本身是一个需要证据的否定性结论（AGENTS §11）：

| 剔除项 | 依据 | 性质 |
| --- | --- | --- |
| NEL / Reporting | `net/features.gni:40` `enable_reporting = !is_ios && !is_minicronet_build` | **条件编译掉**：`set_reporting_service` 与 `set_persistent_reporting_and_nel_store` 整段都在 `#if BUILDFLAG(ENABLE_REPORTING)` 里 |
| DBSC | `net/features.gni:63-64` `enable_device_bound_sessions = !is_minicronet_build && (is_win || is_linux || is_mac)` | **条件编译掉**：宏关闭时 `set_has_device_bound_session_service` 是 `NOTREACHED()`（`url_request_context_builder.h:422`） |
| shared dictionary / shared zstd | `url_request_context_builder.cc:581` `#if !BUILDFLAG(MINICRONET_BUILD)` | **条件编译掉**：`enable_shared_dictionary_` 成为永不读取的成员 |
| 网络质量估计（NQE） | `url_request_context_builder.h:209` 要求外部构造 `NetworkQualityEstimator`，其构造需要 `NetworkQualityEstimatorParams` + `NetLog`（`network_quality_estimator.h:114-116`） | **没有默认实现**：需要宿主进程级的观测设施 |
| net-log | `url_request_context_builder.h:262-264`，注释本身写明「consumers 应自己设 NetLog::Observers」 | **调试设施**，不是线上行为 |

**关键区分：前三项是「编译后不存在」，后两项是「存在但没有意义」。** 差别很重要——
前者即使想做也做不了（除非改 `MINICRONET_BUILD` 的语义），后者是设计取舍。

还有一个容易搞错的地方：`is_minicronet_build` 与 `is_cronet_build` 是**两个独立标志**，
不能假设「凡 cronet 关掉的 minicronet 也关掉」。反例就在同一份 `net/features.gni`：

```gn
# net/features.gni:48  —— 只判断 is_cronet_build
include_transport_security_state_preload_list = !is_cronet_build

# net/features.gni:40  —— 判断 is_minicronet_build
enable_reporting = !is_ios && !is_minicronet_build
```

正因为第 48 行**不**判断 `is_minicronet_build`，HSTS 预置列表在 Core 里是**可用的**
（见 7.1）。判断某一项能力在 Core 里是否存在，必须逐项看 `net/features.gni` 用的是
哪个标志，而不是看项名。`tools/sync-core.sh` 的补丁列表也在做这件事——它手工把
`is_minicronet_build` 并进那些原本只判断 `is_cronet_build` 的位置，HSTS 那行不在补丁里。

剩下的四项按影响排序。

Core 当前已用：`set_user_agent`、`set_accept_language`、`set_enable_brotli`、
`set_enable_zstd`、`set_proxy_config_service`、`set_quic_context`、
`set_ssl_config_service`、`set_http_network_session_params`。

### 7.1 HSTS：Core 完全没有启用（本组最重要）

`grep -rniE 'hsts' core/source/engine.cc` 零命中。

这不是可选优化，是**可观测的协议差异**。真实 Chrome 对 HSTS 站点会把 `http://`
直接升级为 `https://`（不发第一个明文请求），并根据它决定 `Upgrade-Insecure-Requests`
与 `Sec-Fetch-*` 的语义。本库现在会先发明文 `http://`。

**预置列表已经编进产物了。** `net/features.gni:48` 写的是
`include_transport_security_state_preload_list = !is_cronet_build`，而 Core 用的是
`is_minicronet_build = true`（`tools/build-core-linux-x86_64.sh` 的 args.gn），不是
`is_cronet_build`，所以列表是**编译期静态查表**，不需要外部文件。
`GOOGLE_CHROME_BRANDING` 关掉的只是 static pins（`transport_security_state.cc:242-244`），
不是 HSTS 列表。

**但完全没接。** builder 在 `url_request_context_builder.cc:416-417` 无条件执行
`context->set_transport_security_state(make_unique<TransportSecurityState>(bypass_list))`，
所以 `TransportSecurityState` **对象是存在的**；缺的是两件事：

1. `URLRequest::StartOnNetworkThread` 里的 scheme 升级决策没有查它；
2. HSTS 响应头（`Strict-Transport-Security`）没有被记录。

```c
/* mn_engine_config_t 末尾追加 */
/* 0 = 关（CLI 访问内网自签名 HTTP 的常见场景需要它），1 = 开（与 Chrome 一致，默认） */
uint8_t hsts_enabled;
uint8_t hsts_reserved[3];
/* 逗号分隔的 bypass 列表，对应 set_hsts_policy_bypass_list。空 = 不绕过 */
const char *hsts_bypass_list;
size_t hsts_bypass_list_length;
```

默认开，但**必须能关**：命令行客户端经常要访问内网 HTTP 服务，HSTS 强制升级会让
`http://intranet/` 直接失败。这与 Chrome 的行为一致——Chrome 也是默认开，只是用户不能关，
而库的调用方需要能关。

**不影响已冻结的指纹**：HSTS 只改 URL scheme 决策与证书错误的致命性判断，不碰
ClientHello、ALPN 或 H2 SETTINGS。

### 7.2 Cookie 访问语义：默认不是 Chrome 的行为

```c
net/cookies/cookie_monster.cc:474-498   构造时不带 delegate
net/cookies/cookie_access_delegate.h:21   接口存在，但 builder 不暴露 setter
```

没有 delegate 时 `GetAccessSemantics` / `GetScopeSemantics` 走默认实现
（`CookieAccessSemantics::UNKNOWN`、非 legacy scope）。真实 Chrome 在
`services/network` 里注入自己的 delegate，实施 SameSite 默认值（Lax-by-default）、
分区 cookie（CHIPS）与第三方 cookie 策略。

后果：**同一份 `Set-Cookie` 在 Chrome 与本库上可能落进不同的 SameSite 桶**，于是
「哪些请求会带 cookie」不同。在反爬对抗里这与指纹同等致命——cookie 不带，就被判为非浏览器。

**这一项不加策略开关。** 加 `samesite_policy=` 之类的参数等于在 Core 里重写 Chrome 的
策略引擎，是 §1 禁止的事。正确做法是在 Core 内注入一个实现 Chrome 默认语义的
`ChromeDefaultCookieAccessDelegate`，让默认行为对齐；ABI 至多留一个显式覆盖：

```c
/* 0 = 跟随 Chrome 默认（Lax-by-default），1 = 强制 None，2 = 强制 Lax，3 = 强制 Strict */
uint8_t cookie_samesite_override;
uint8_t cookie_samesite_reserved[3];
```

默认 0，即调用方什么都不传时行为与 Chrome 一致。这一项的价值不在可配置，而在**默认对齐**。

### 7.3 `HttpNetworkSessionParams`：默认值与 Chrome 不同

Core 只在 `set_http_network_session_params` 里设了 `protocol_mode` 与
`ignore_certificate_errors`，其余字段全是 C++ 默认。真实 Chrome 走
`components/network_session_configurator/browser/network_session_configurator.cc:754`
的 `ParseCommandLineAndFieldTrials`，其中三个字段直接影响 **H2 帧序列**：

| 字段 | C++ 默认 | Chrome 可能启用 | 影响的帧 |
| --- | --- | --- | --- |
| `enable_http2_settings_grease` | `false` | 视 field trial 为 `true` | SETTINGS 里的 GREASE id |
| `greased_http2_frame` | 无 | 插入随机类型帧 | 握手后额外帧 |
| `http2_end_stream_with_data_frame` | `false` | `true` | 用空 DATA 结尾而非 HEADERS END_STREAM |

**这一项不加 ABI 字段。** 让调用方能改这些值就是让调用方能污染已冻结的指纹。
正确做法是把它们**纳入 `RuntimeProfileData`**，由 `tools/generate-*` 从抓包与源码推导——
这是数据问题，不是接口问题。

具体要做的两件事：
1. `core/source/minicronet/runtime_profile_data.h` 加三个字段；
2. `Engine::InitializeOnNetworkThread` 把 profile 值写进 `session_params`，
   `chromium_current` 保持 C++ 默认（那正是「current」的含义）。

### 7.4 代理委托：多跳代理链缺一环

Core 有 `set_proxy_config_service`，`mn_engine_config_t` 也有 `proxy_username` /
`proxy_password`。缺的是 `set_proxy_delegate`（`url_request_context_builder.h`）——真实
Chrome 用它做 **proxy chain 决策**：多跳代理、DIRECT fallback 顺序、以及按目标
scheme 选择链路。

单跳代理够用，所以这一项优先级最低。但如果项目的使用场景包含代理池（反爬标配），
它迟早会被用到。

```c
/* 有序的代理链，每条是 "scheme://host:port"，逗号分隔。
 * 空 = 沿用现有单跳语义（proxy_rules），行为与 v8 一致。 */
const char *proxy_chain;
size_t proxy_chain_length;
```

注意：这一项与 host mapping（第 5 项）有一个已知交互——`ssl_connect_job.cc:412` 有一句
`DCHECK_EQ(params_->GetConnectionType(), SSLSocketParams::DIRECT)`，注明 ECH 的 DNS 覆盖
「只对直连生效」。所以 host mapping 与代理链不能同时为 ECH 服务，这条要写进文档。

### 7.5 验收用例

```python
# 1. HSTS：预置列表命中时应直接 HTTPS，不发明文
with chrome_client.Session() as s:           # hsts_enabled 默认 1
    # 用一个在预置列表里的 host；服务端只监听 TLS
    s.get("http://paypal.com/", timeout=5)
assert server.saw_plaintext_http == 0        # 一次明文都没有
```

```python
# 2. HSTS 可关：内网自签名 HTTP 必须能直接访问
with chrome_client.Session(hsts=False) as s:
    assert s.get("http://intranet.local/").status_code == 200
```

```python
# 3. SameSite 默认与 Chrome 一致：不写 SameSite 的 Set-Cookie 应视为 Lax
with chrome_client.Session() as s:
    s.get(base + "/set-cookie-without-samesite")
    cookie = next(c for c in s.cookies if c.name == "s")
assert cookie.same_site == "Lax"              # Chrome 的默认，不是 None
```

```python
# 4. H2 session params 跟随 profile：chrome_124 与 chrome_155 的 END_STREAM 选择
#    可能不同，抓包对比可验证；至少断言 chromium_current 与 v8 行为一致
a = chrome_client.get(url, impersonate="chrome_124")
b = chrome_client.get(url, impersonate="chrome_155")
# 具体断言取决于 profile 数据，此处只固定「两者来自各自 profile 而非硬编码」
```

### 7.6 风险

| 风险 | 处理 |
| --- | --- |
| HSTS 默认开发导致内网 HTTP 失败 | 必须提供关闭开关，且文档要把它写在最显眼处 |
| Cookie delegate 改变已有行为 | 这是**有意的行为变更**：现状与 Chrome 不一致。需要在 release note 里明写，并给出对照用例 |
| H2 params 纳入 profile 后需重新取证 | 72 个 profile 的三级证据链要重新跑一遍第 2、3 级；第 1 级（源码）本来就该覆盖这些字段 |
| 代理链与 host mapping 的 ECH 交互 | 写进文档：两者不能同时为 ECH 服务（`ssl_connect_job.cc:412` 的 DCHECK） |

---

## 8. 平台身份：`platform_override` 与默认 UA

这一项来自两个实测发现，指向同一个根因：**平台身份由三个字段共同表达，但它们现在
各自独立决定，可以互相矛盾。**

### 8.1 现状（实测）

`impersonate="chrome_124"` 在 Linux 上发出的平台身份是**一致的**，这条路径是对的：

```text
User-Agent          Mozilla/5.0 (X11; Linux x86_64) ... Chrome/124.0.0.0 ...
sec-ch-ua-platform  "Linux"
sec-ch-ua-mobile    ?0
sec-ch-ua           "Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"
```

平台 token 跟随宿主，版本号跟随 profile。`browser_headers.py:48-68` 与
`core/source/minicronet.cc:143-151` 各有一份推导，字符串必须逐字相同——这是刻意的
双份，因为 UA 的最终值由 Core 决定，而其余头由 facade 决定。

**但传参 UA 会打破这个一致性。** 实测：

```text
impersonate=chrome_124 + headers={"User-Agent": "...Windows NT 10.0; Win64; x64..."}
  -> User-Agent:          Windows NT 10.0; Win64; x64 ...
     sec-ch-ua-platform:  (消失)

impersonate=chrome_124 + headers={"User-Agent": "...iPhone; CPU iPhone OS 17_0..."}
  -> User-Agent:          iPhone ...
     sec-ch-ua-platform:  (消失)
     sec-ch-ua-mobile:    ?0        ← 与 iPhone 矛盾
```

即：UA 与 client hints 之间没有任何约束。一个只看 `sec-ch-ua-platform` 的服务端会看到
`"Linux"`（或什么都没有），只看 UA 的会看到 `Windows`。**任意一方都能判定这不是真
浏览器**，这正是 `docs/LESSONS.md` 第 46 条点名的「比缺一个 hint 更容易被检出」。

### 8.2 默认 UA 是库名，不是 Chrome

`impersonate=None`（默认 `chromium_current`）实测发出的 UA 是：

```text
minicronet/0.2.9
```

来自 `core/source/minicronet.cc:48` 的 `kDefaultUserAgent = "minicronet/" MINICRONET_VERSION`。
这是全库最容易暴露身份的字符串——任何看一眼 UA 的服务端立即知道不是浏览器。

必须区分两种情况下它为什么出现，因为修法不同：

| 情况 | 现状 | 应该是什么 |
| --- | --- | --- |
| 历史 profile（chrome_86..157） | **不会**出现：`HistoricalUserAgent()` 覆盖了它 | 保持 |
| `chromium_current`（默认） | 出现 | 应等于某个具体 Chrome 版本的 UA |

`minicronet.cc:360-367` 的逻辑是：只在 `!profile.is_current()` 时用
`HistoricalUserAgent()` 覆盖。所以 `chromium_current` 保留了默认值。而「current」的语义
本该是「像这个构建所基于的 Chromium 版本」，那个版本号是已知的——`CHROMIUM_REVISION`
与 `kFullVersions` 表都在仓库里，缺的只是把它用在这个分支上。

### 8.3 设计：平台身份必须由**一个**值派生

朴素的想法是「传参 UA 优先，但按当前平台自动修正」。**这个做法会砍掉一个真实需求**：
调用方传一个 Windows UA，很可能就是想让这个请求看起来来自 Windows，而不是想在本机
被改写回 Linux。自动修正让他无法表达这个意图。

所以必须把「我只要某个版本的 Chrome」和「我要伪装成某个平台」分成两件事：

| 意图 | 参数 | 结果 |
| --- | --- | --- |
| 某个版本的 Chrome，平台跟本机 | `impersonate="chrome_124"` | UA group / platform / mobile 都是本机（**已实现**） |
| 伪装成 Windows 的 Chrome 124 | `platform_override="windows"` | 三者一起切到 Windows（**本项新增**） |
| 只换那个字符串，其余不动 | `headers={"User-Agent": ...}` | 原样发送（**保持现状**，文档要写明它会与 hint 矛盾） |

第三种不该改：它现在是「我要完全自己控制」的逃生口。但它必须**在文档里承担后果**，
不能悄悄变成「自动修正」。

### 8.4 ABI 设计

`mn_engine_config_t` 末尾追加一个枚举。**不需要新导出符号**：`platform` 只影响
`HistoricalUserAgent()` 的 `kPlatform` 分支与 facade 的两个 hint，全是构造期决定。

```c
typedef enum mn_platform {
  MN_PLATFORM_AUTO = 0,          /* 跟随宿主，与 v8 行为一致 */
  MN_PLATFORM_WINDOWS = 1,
  MN_PLATFORM_MACOS = 2,
  MN_PLATFORM_LINUX_X86_64 = 3,
  MN_PLATFORM_LINUX_AARCH64 = 4,
} mn_platform_t;

/* mn_engine_config_t 末尾追加 */
mn_platform_t platform;
```

为什么是 engine 级而不是 per-request：平台身份必须与 TLS 指纹、HTTP/2 指纹一起固定。
允许逐请求切换会产生「同一个 Engine 的两次请求声称不同平台」，而 Engine 的
`URLRequestContext`（含 TLS session cache、连接池）是共享的——那本身就是一个可检出的矛盾。

**Core 侧实现只有一处改动**：`HistoricalUserAgent()` 里那段 `#if BUILDFLAG(IS_WIN) /
#elif ...` 从编译期分支改成运行期查表（`minicronet.cc:143-151`）。`kReducedUserAgentFirstMajor`
与前缀后缀都不动，所以版本串行为不变。

**facade 侧**：`_platform_token()` 与 `_sec_ch_ua_platform()` 接受 override；
`_sec_ch_ua_mobile()` 由平台决定（桌面一律 `?0`，无移动端取值，因为本库不支持移动平台）。

### 8.5 默认 UA 的修法

`chromium_current` 应当报告**这个构建所基于的 Chromium 版本**的 UA，而不是库名。
依据已经存在：`core/binaries/*/manifest.json` 记录了 `chromium_revision`，
`kFullVersions` 表记录了 86–104 的完整版本串，而 105+ 的规则是 `<major>.0.0.0`。

两种可选做法，必须选一个并写清楚：

| 做法 | 优点 | 缺点 |
| --- | --- | --- |
| A. `chromium_current` 沿用最新已审计 profile 的版本串 | 与 profile 表同源，可门禁 | 「current」被解释成「最新已知」而非「构建所基于」 |
| B. 从 `chromium_revision` 推出主版本号再拼 | 语义严格 | Chromium 检出只有一个 commit，主版本需要额外映射表 |

**建议 B**，因为「current」的字面意思就是当前构建。若映射表拿不到，退回 A 并在文档写明。

无论选哪个，`minicronet/<version>` **不应出现在任何默认路径上**。它是库标识，属于
`core_version()`，不属于网络身份。

### 8.6 门禁：现在没有任何检查断言这三者一致

这是本项最值得先补的部分。`bindings/python/tests/test_client_hints.py:160-167` 的
`test_the_platform_token_matches_the_cores_derivation` 只断言字符串**前缀**，
从来没有比对过 `core/source/minicronet.cc` 里那份 C++ 字面量。于是：

- 两端推导可以静默分叉；
- 传参 UA 与 hint 可以互相矛盾；
- 默认 UA 可以是库名。

三件事都没有门禁。新增 `tools/audit-platform-identity.py`：

1. 从 `minicronet.cc` 抽出 `kPlatform` 的每个分支值（含 `#if` 顺序）；
2. 与 `browser_headers.py` 的 `_platform_token()` 逐分支比对；
3. 对每个 `mn_platform_t` 取值构造一次请求，断言 UA group、`sec-ch-ua-platform`、
   `sec-ch-ua-mobile` 三者由同一个值派生；
4. 断言没有任何默认路径发出 `minicronet/`。

第 4 条是变异测试的好目标：把 `kDefaultUserAgent` 改回库名，第 4 条必须失败。

### 8.7 验收用例

```python
# 1. 平台身份三者一致（每个 override 一次）
for plat, expect_token, expect_hint, expect_mobile in (
        ("windows", "Windows NT 10.0; Win64; x64", "Windows", "?0"),
        ("macos", "Macintosh; Intel Mac OS X 10_15_7", "macOS", "?0"),
        ("linux", "X11; Linux x86_64", "Linux", "?0")):
    with chrome_client.Session(impersonate="chrome_124",
                               platform_override=plat) as s:
        s.get(echo_url)
    assert expect_token in last["User-Agent"]
    assert last["sec-ch-ua-platform"] == '"%s"' % expect_hint
    assert last["sec-ch-ua-mobile"] == expect_mobile
```

```python
# 2. auto 仍然跟随宿主，与 v8 一致
with chrome_client.Session(impersonate="chrome_124") as s:
    s.get(echo_url)
assert last["sec-ch-ua-platform"] == '"%s"' % host_platform()
```

```python
# 3. 默认 UA 不再是库名
with chrome_client.Session() as s:          # chromium_current
    s.get(echo_url)
assert "minicronet/" not in last["User-Agent"]
assert "Chrome/" in last["User-Agent"]
```

```python
# 4. 显式传 UA 仍然原样发送（逃生口不能变成自动修正）
with chrome_client.Session(impersonate="chrome_124",
                           headers={"User-Agent": "custom/1.0"}) as s:
    s.get(echo_url)
assert last["User-Agent"] == "custom/1.0"
```

第 1 与第 4 条是一对：前者要求 override 生效，后者要求传参不被改写。两条同时绿，
才能证明「平台可切换」和「调用方仍能完全控制」这两件事都成立。

### 8.8 风险

| 风险 | 处理 |
| --- | --- |
| `platform_override` 与其他指纹冲突 | 只切换 UA/hint 不会改 TLS 或 H2 指纹，所以「声称 Windows 但 TLS 是 Linux 构建」仍是可检出的。文档必须写明：它只对齐**能被 UA 一致性检查到的那部分**，不改变底层指纹 |
| 与 profile 的 `user_agent` 冲突检测交互 | `minicronet.cc:361-363` 在 `!profile.is_current()` 时对不匹配的 `user_agent` 返回 `MN_ERROR_PROFILE_CONFLICT`。`platform_override` 会让 UA 与 profile 记录不同——必须扩展该检查，把「平台不同但版本相同」视为合法 |
| 默认 UA 变更影响既有用户 | 它是有意的破坏性变更，进 release note；提供 `user_agent=` 逃生口（已存在） |

---

## 9. 非幂等请求不得被静默重发（需要 Chromium 补丁）

这一项是**缺陷修复**，不是能力补齐。第 7 节里剔掉的那些是「编译后不存在」，这一项是
「存在、且行为有语义错误」。

### 9.1 实测：POST 会被重发

构造：服务端第一条请求正常响应（让连接进入 keep-alive 池），第二条请求在**同一条复用
连接**上直接关闭、不返回任何响应。

```text
GET   -> [GET /warm, GET /drop, GET /drop]      重发 1 次，最终 200
POST  -> [GET /warm, POST /drop, POST /drop]    重发 1 次，最终 200

body 也逐字节重发，不是空包：
POST /drop  body: (len=7, b"payload")
POST /drop  body: (len=7, b"payload")     <- 第二次
```

三重叠加：

1. **`retry=0` 拦不住**。`RetryStrategy` 只管状态码重试，对 Chromium 的传输层重发
   完全无感知。调用方显式说「不重试」，POST 仍被发两次。
2. **不可重放的 body 也会被重发**。generator 体（chunked）同样看到 2 次 POST，
   facade 的 `_body_can_be_resent` 守卫在这里不起作用，因为重发发生在 Core 层。
3. **根因是判据不看幂等性**（`net/http/http_network_transaction.cc:2388-2396`）：

```c
bool connection_is_proven = stream_->IsConnectionReused();
bool has_received_headers = GetResponseHeaders() != nullptr;
return connection_is_proven && !has_received_headers;
```

只问「连接是否复用」「是否收到响应头」。方法是否幂等、body 能否重放，一概不问。
上游注释的推理是「缓存里的 keep-alive 连接迟早用完，所以不会无限循环」——对 GET 成立，
对 POST 是语义错误：重发一次就是一次重复提交。

还有一个放大因素：`SocketReuseType` 的三个取值里 **`kUnusedIdle` 也算作复用**
（`http_basic_state.cc:86-90`），所以一个**从未承载过请求**的空闲连接同样会触发重发。

### 9.2 不 patch 无法关闭（已逐层验证）

| 路径 | 结论 | 依据 |
| --- | --- | --- |
| `URLRequest` 配置 | 无字段 | `grep -nE "disable_retry\|no_retry\|max_retries\|retry" url_request.h` 只有 Storage Access 的两条 |
| `LoadFlags` | 无此位 | `net/base/load_flags_list.h` 全部 20 位均不影响池的复用决策；`LOAD_IGNORE_LIMITS` 只影响连接数上限 |
| `HttpNetworkSession` 参数 | 无 | `http_network_session.h` 无 retry 字段 |
| Core 回调拦截 | 不可行 | 重发发生在 `HttpNetworkTransaction` 内部，Core 的 `on_response` 收不到第一次失败的信号 |
| 发 `Connection: close` 规避 | 无效 | 该头不改变连接生命周期（另一项已实测） |
| **Chromium 补丁** | **唯一可行** | —— |

`ShouldResendRequest()` 是 private 成员、唯一调用点在 `DoLoop` 内，没有虚函数、配置或
回调通道。所以这一项必须维护一个 `net/` 层补丁。

### 9.3 补丁内容

`HttpUtil::IsMethodIdempotent`（`net/http/http_util.h:119`）已经实现了 RFC 7231 §4.2.2
的判定，直接复用，不要自己写一份方法表。

```diff
--- a/net/http/http_network_transaction.cc
+++ b/net/http/http_network_transaction.cc
@@ bool HttpNetworkTransaction::ShouldResendRequest() const {
   bool connection_is_proven = stream_->IsConnectionReused();
   bool has_received_headers = GetResponseHeaders() != nullptr;
+
+  // MINICRONET: a non-idempotent request must never be resent. The origin may
+  // already have processed the first copy, and nothing above this layer can
+  // undo that: a client library exposes URLRequest directly, with none of the
+  // browser-side prompts that make this safe in Chrome. Failing with a
+  // connection error is recoverable; a silent duplicate POST is not.
+  if (request_ &&
+      !HttpUtil::IsMethodIdempotent(request_->method)) {
+    return false;
+  }

   // NOTE: we resend a request only if we reused a keep-alive connection.
   // This automatically prevents an infinite resend loop because we'll run
   // out of the cached keep-alive connections eventually.
   return connection_is_proven && !has_received_headers;
 }
```

两个实现细节，都是从源码读出来的，不是猜的：

1. **必须用 `request_->method`，不能用 `request_method_`。** 后者在
   `http_network_transaction.h:472` 且**只在 `#if BUILDFLAG(ENABLE_REPORTING)` 内**
   （`:465-476`），而 Core 构建里 `enable_reporting = !is_ios && !is_minicronet_build`
   （`net/features.gni:40`）为 `false`——用它编译不过。
2. **`request_` 在此处仍然有效。** 它在 `:420` 赋值、`:1794` 才置空，而
   `ShouldResendRequest()` 由 `:2194` 调用。但仍加 `request_ &&` 守卫，因为置空路径
   以后可能变化。

### 9.4 `sync-core.sh` 的接入

新增 `core/patches/no-resend-non-idempotent.patch`，在 `sync-core.sh` 的 `apply_patch`
列表里加一行，marker 用补丁里那行注释的固定片段：

```bash
apply_patch no-resend-non-idempotent.patch . \
  'net/http/http_network_transaction.cc:MINICRONET: a non-idempotent request'
```

现有机制会自动提供三件事（`sync-core.sh` 已有）：正向应用校验、
`git apply --reverse --check` 判断是否已完整存在、以及拒绝「部分应用」的中间态。

### 9.5 9 个平台重建

这不像 ABI 变更需要改结构体，但**它会改变产物的行为与哈希**，所以九个平台都要重建。
判据与前几项一致：**任何一个 manifest 的 sha256 没变，就说明那个平台没重建**。

| # | 目录 | 需要重建 |
| --- | --- | --- |
| 1–9 | `linux-x86` / `linux-x86_64` / `linux-armv7` / `linux-arm64` / `windows-x86` / `windows-x86_64` / `windows-arm64` / `macos-x86_64` / `macos-arm64` | 全部，因为 `http_network_transaction.cc` 是共用源码 |

注意：这一项**不改 ABI**，所以 `MN_ABI_VERSION` 不必为了它单独动。它与 v9 的 ABI 变更
同批发布只是排期选择，不是版本依赖——需要的话它可以单独出一个补丁版本。

### 9.6 验收用例

沿用 9.1 的服务端形态：第一条请求正常响应使连接入池，第二条在复用连接上直接关闭。

```python
# 1. 非幂等请求不再被重发
for method in ("post", "put", "patch", "delete"):          # PUT/DELETE 幂等，见 #2
    seen.clear()
    with chrome_client.Session(retry=0) as s:
        s.get(base + "/warm")
        try:
            getattr(s, method)(base + "/drop", data=b"payload")
        except chrome_client.ConnectionError:
            pass
    assert sum(1 for r in seen if r.startswith(method.upper())) == 1, (method, seen)
```

注意 **PUT / DELETE 是幂等的**（RFC 7231 §4.2.2），它们**会**被重发，这是正确行为。
所以第 1 条要断言的是「POST 不重发」，而不是「所有非 GET 都不重发」：

```python
# 1b. 精确断言：POST 不重发，PUT 仍然重发
for method, expect in (("post", 1), ("put", 2)):
    seen.clear()
    with chrome_client.Session(retry=0) as s:
        s.get(base + "/warm")
        try:
            getattr(s, method)(base + "/drop", data=b"payload")
        except chrome_client.ConnectionError:
            pass
    n = sum(1 for r in seen if r.startswith(method.upper()))
    assert n == expect, (method, n, expect)
```

```python
# 2. 失败要变成可处理的错误，而不是静默成功
with chrome_client.Session(retry=0) as s:
    s.get(base + "/warm")
    with pytest.raises(chrome_client.ConnectionError):
        s.post(base + "/drop", data=b"payload")
```

第 2 条是这次取舍的核心：**从「可能重复提交」变成「可能因连接重置而失败」**。
失败是可处理的（调用方可以决定要不要自己重试），重复提交不可撤销。

### 9.7 取舍与风险

| 风险 | 说明与处理 |
| --- | --- |
| 非幂等请求的健壮性下降 | 这是**有意的**：从前是「可能自动恢复但可能重复提交」，现在是「明确失败」。文档与 release note 必须写明，并指出调用方可用 `retry=` 自行重试 |
| PUT / DELETE 仍会重发 | 正确行为（幂等），不要一起关掉。用例 1b 把这条钉住，防止后来的人「顺手全关」 |
| 与上游分叉 | 这是本项目第 4 个 `net/` 层行为补丁。`sync-core.sh` 已有 marker 校验与部分应用拒绝，但上游改这段代码时会冲突——升级 Chromium 时需先复查 |
| 不影响 GET 的重发 | GET 重发是无害的，保留它维持既有健壮性 |

### 9.8 与第 2 节 `mn_request_retry_count()` 的关系

打了这个补丁之后，重发只对幂等方法发生，于是 `retry_count` 的定位从「安全判据」降为
「诊断信息」。**结论：仍然值得加，但优先级下调。**

理由有三：

1. **GET 仍会重发**（`kMaxRetryAttemptsOnConnectionErrors = 50`），调用方拿到
   `ERR_EMPTY_RESPONSE` 时仍需要知道「Chromium 已经试过几次」才能判断是网络问题还是
   服务端问题。
2. 服务端日志与客户端报告对不齐的问题依旧存在：抓包看到 3 次请求，客户端报 1 次失败。
3. 它是第 2 节那个 getter 的**顺带产物**（同一次 `SSLInfo`/`HttpResponseInfo` 读取路径），
   增量成本接近零。

所以要做的顺序是：**先打 9 的补丁（安全），再加 2 的计数器（可观测）**。反过来会让一个
可观测但仍有重复提交风险的版本先上线。

---

## 10. `Connection` 头：能发出去，但改变不了任何行为

这一项与第 9 项同类——**行为与声明不符**——但不需要 Chromium 补丁，只需要在绑定层
fail-closed。它比第 9 项轻，但同样是「看起来生效、实际无效」。

### 10.1 实测：三种结果，全都不对

| 写法 | 现状 | 应该 |
| --- | --- | --- |
| `headers={"Connection": None}` | 抛 `UnsupportedFeature` | 保持（已是 fail-closed） |
| `headers={"Connection": "close"}` | **接受，发出去了** | 抛错或真正关闭连接 |
| `headers={"Connection": "keep-alive"}` | 接受，发出去了 | 无害但无意义 |

而「发出去了」并不意味着生效。实测连接复用：

```text
default                -> 3 次请求共用 1 条连接
Connection: keep-alive -> 3 次请求共用 1 条连接
Connection: close      -> 3 次请求共用 1 条连接     <- 应该是 3 条
```

服务端侧确认它确实收到了（`self.headers.get("Connection") == "close"`，且
`close_connection` 被置为 `True`），但 Chromium 仍然复用这条连接再发第二次请求。

**根因**：`Connection` 是 hop-by-hop 头，连接生命周期由 `HttpStreamPool` 决定；
`SetExtraRequestHeaders` 只是把字符串加到请求头上，不参与连接池决策。所以它是一个
「能发出去、但改变不了任何行为」的头。

### 10.2 更严重的一点：h2/h3 上它被静默丢弃

`net/spdy/spdy_http_utils.cc:214-221` 在构造 HTTP/2 头块时直接跳过：

```c
if (name.empty() || name[0] == ':' || name == "connection" ||
    name == "proxy-connection" || name == "transfer-encoding" ||
    name == "host") {
  continue;                    // 静默丢弃，不报错
}
```

所以同一个请求在 h1 上会发出 `Connection: close`、在 h2 上不会——**行为随协商协议
静默变化**。而 RFC 9113 §8.2.2 与 RFC 9114 §4.2 都明确禁止该字段出现在 h2/h3 上，
丢弃是协议正确的，问题在于**调用方无从知晓自己的设置被忽略了**。

### 10.3 修法：绑定层 fail-closed，不加 ABI

不需要新 ABI 字段。`Connection` 在 Core 里没有任何合法的可配置语义，所以正确做法是
**整头拒绝**，与其他 Core 自有头一致。

现状已经有一半：`_CORE_OWNED_HEADERS`（`sessions.py:210-218`）包含 `connection`，
`_reject_core_owned_removals` 会拦住删除。**缺的是赋值路径**——`setdefault`/合并只挡
`None`，不挡具体取值。

改动点：在 `_prepare_call` 里，与 `_reject_core_owned_removals` 并列加一个
`_reject_hop_by_hop_headers`，拦截**所有** hop-by-hop 头的赋值：

```python
#: RFC 9110 §7.6.1 的 hop-by-hop 集合，减去本库真正支持的那些。
#: Connection / Proxy-Connection / Keep-Alive / TE / Trailer / Transfer-Encoding /
#: Upgrade 都由 Chromium 的连接层决定，extra headers 改不动它们。
_HOP_BY_HOP_HEADERS = frozenset((
    "connection",
    "proxy-connection",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
))


def _reject_hop_by_hop_headers(headers, method):
    """Fails closed for headers that only the connection layer can honour."""
    if headers is None:
        return
    offending = sorted(name for name in headers.deleted_names | set(headers.keys_lower())
                       if name in _HOP_BY_HOP_HEADERS)
    if not offending:
        return
    raise UnsupportedFeature(
        "%s cannot be set: Chromium owns connection lifetime, and extra headers do "
        "not reach the connection pool. On HTTP/2 and HTTP/3 these fields are "
        "discarded outright (RFC 9113 §8.2.2, RFC 9114 §4.2), so accepting them "
        "would silently change behaviour with the negotiated protocol."
        % (", ".join(offending),))
```

`transfer-encoding` 与 `content-length` 不放进这个新集合，因为它们已经由
`_CORE_OWNED_HEADERS` 的删除路径覆盖，且赋值有正当用途（调用方指定 body 长度）。

### 10.4 验收用例

```python
# 1. 所有 hop-by-hop 头的赋值都抛错
for name, value in (("Connection", "close"), ("Connection", "keep-alive"),
                    ("Keep-Alive", "timeout=5"), ("Upgrade", "h2c"),
                    ("Proxy-Connection", "keep-alive"), ("TE", "trailers")):
    with chrome_client.Session() as s:
        with pytest.raises(chrome_client.UnsupportedFeature):
            s.get(url, headers={name: value})
```

```python
# 2. 删除路径仍然抛错（不变）
with chrome_client.Session() as s:
    with pytest.raises(chrome_client.UnsupportedFeature):
        s.get(url, headers={"Connection": None})
```

```python
# 3. 不是 hop-by-hop 的头不受影响
with chrome_client.Session(impersonate="chrome_124") as s:
    s.get(echo_url, headers={"X-Custom": "1", "Content-Type": "text/plain"})
assert last["X-Custom"] == "1"
assert last["Content-Type"] == "text/plain"
```

### 10.5 风险与性质

| 项 | 说明 |
| --- | --- |
| 破坏性 | 任何现在传 `Connection: close` 且「以为它生效」的代码会开始收到异常。这是对的——它从来没生效过 |
| 不加 ABI | 不需要新符号、不需要重建 9 个平台、不需要动 `MN_ABI_VERSION` |
| 与第 9 项的区别 | 第 9 项需要 Chromium 补丁（行为在 C++ 层）；第 10 项只需绑定层抛错（该头在 C++ 层没有任何合法语义） |
| 与 h2/h3 的关系 | 拒绝它也顺带修掉了「h1 发出去、h2 静默丢弃」的协议相关行为差异 |

---

## 11. 发布必须同步的载体（9 个平台）

ABI 版本从 8 到 9 不是一个数字的改动。以下每一处都必须同一次提交内改完，
否则 `tools/audit-abi.sh` 会失败——这是设计意图，不是障碍。

### 11.1 源头与声明

| 载体 | 改什么 |
| --- | --- |
| `core/abi/minicronet.h` | `MN_ABI_VERSION` 8u → 9u；新增 5 个符号声明、`mn_cookie_t` / 两个枚举 |
| `crates/minicronet-sys/src/lib.rs` | 5 个 `pub fn`；`#[repr(C)]` 新结构；`abi_layout_tests` 增补 |
| `core/source/minicronet.cc` | 5 个 `MN_CALL` 定义 |
| `core/source/engine.cc` / `request.cc` | cookie 转发；`connection_info` 读取；`set_host_mapping_rules` / `set_enable_client_hints` / DNS overrides 三条 config 字段 |
| `tools/audit-abi.sh` | `symbols=(` 数组加 5 项；版本断言 8u → 9u；输出文案里的 "ABI v8" |
按项拆开看，哪些进导出表、哪些只进结构体：

| 项 | 新增导出符号 | 新增 `mn_engine_config_t` 字段 |
| --- | --- | --- |
| 1 Cookie 存取 | 4 | 0 |
| 2 协商协议 | 1（getter） | 0 |
| 3 Client hints | 0 | 1（`inject_client_hints`） |
| 4 DNS 配置 | 0 | 2 指针 + 4 字节标志 + 2 字段（DoH 模板） |
| 5 Host mapping | 0 | 2（`host_mapping_rules` + 长度） |
| 6 mTLS | 2（set / clear） | 0 |
| 2.5 ECH 状态 | 1（getter） | 0 |
| 7.1 HSTS | 0 | `hsts_enabled` + `hsts_bypass_list`（2 字段） |
| 7.2 Cookie 语义 | 0 | `cookie_samesite_override`（1 字段） |
| 7.3 H2 session params | 0 | 0（进 `RuntimeProfileData`，不是 ABI） |
| 7.4 代理链 | 0 | `proxy_chain` + 长度（2 字段） |
| 8 平台身份 | 0 | `platform`（1 个 `mn_platform_t`） |
| 9 非幂等重发 | 0 | 0（Chromium 补丁，不动 ABI） |
| 10 hop-by-hop 头 | 0 | 0（纯绑定层 fail-closed） |

合计：**8 个新符号（20 → 28）**——4 个 cookie、1 个协商协议、1 个 ECH 状态、2 个 mTLS。
第 7 节的四项与第 8、9、10 节**一个符号都不加**，只追加 `mn_engine_config_t` 字段（HSTS 2 个、
cookie 1 个、代理链 2 个、platform 1 个），H2 session params 走 profile 数据所以一个字段都不加。
`mn_engine_config_t` 从 152 字节（64 位）增长约 56 字节。
`mn_request_config_t`（144）与 `mn_request_callbacks_t`（48）**不变**，所以 32 位分支的
92 / 28 两条断言不用动，只有 `mn_engine_config_t` 需要重算。

### 11.2 导出表（三份）

| 文件 | 改什么 |
| --- | --- |
| `core/exports/minicronet.lds` | `MINICRONET_2 { ` → `MINICRONET_3 {`，加 5 个符号 |
| `core/exports/minicronet.def` | `EXPORTS` 段加 5 个符号 |
| `core/exports/minicronet.exports` | 加 5 个符号 |

### 11.3 九个平台产物与 manifest

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

### 11.4 布局断言

`sizes_and_enum_values_match_public_header`（`crates/minicronet-sys/src/lib.rs:791`，断言在 792-815）
的两套断言都要跟着改：

- 64 位：`mn_engine_config_t` 152（DNS 追加 2 指针 + 4 字节标志，按 8 字节对齐
  需重算）、`mn_request_config_t` 144（不变）、`mn_cookie_t` 需新增
- 32 位：`mn_engine_config_t` 84、`mn_request_config_t` 92（不变）、`mn_cookie_t` 需新增

**注意**：32 位分支目前没有 CI 执行路径（见 `NEXT_STEPS.md`「32 位 ABI 布局没有执行路径」）。
这不是本提案能解决的，但本提案会让那个分支再增两处需要手算的断言——
建议在同一次 v9 里把该分支接上 `cargo test --target i686-unknown-linux-gnu`（可跑）。

### 11.5 文档

`docs/ABI.md`（新增 v9 章节）、`docs/COMPATIBILITY_BOUNDARY.md`（`http_version`、
cookie 双 store 两节作废）、`docs/NEXT_STEPS.md`（把 2 节标为已完成）、
`README.md` / `README.en.md` / `bindings/python36/README.md`（能力表）、
`AGENTS.md:36`（"当前 ABI v8，**20** 个导出符号" → v9，24 个）。
`docs/review-<version>/` 的 JSON 重新生成。

---

## 12. 验收用例（第 1、2、5 项）

本节三条覆盖 cookie、协商协议与 host mapping。其余各项的用例在各自小节里：
ECH 状态见 2.5、mTLS 见 6.5、client hints 见 3.2、第 7 节四项见 7.5、平台身份见 8.7、
非幂等重发见 9.6、hop-by-hop 头见 10.4。

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

## 13. 顺序与风险

建议的落地顺序，按"能否独立验证"排：

0. **第 9 项与第 10 项必须最先做**——它们是十项里仅有的两个**语义缺陷**（静默重复提交、
   静默无效头），其余都是能力缺失或保真度不足。第 10 项只需绑定层改动，可以立即做；
   第 9 项需要 Chromium 补丁，但两者都不改 ABI，可以单独出一个补丁版本，不必等 v9。
1. **第 2 项（协商协议 + 2.5 ECH 状态）先做**——三个 getter、不改任何结构体布局，
   32 位断言不用动，风险最低，且协商协议是唯一无替代实现的一项。
2. **第 6 项（mTLS）第二**——两个 setter、注入点是 public 的、不新增结构体字段，
   且不改变 ClientHello 指纹；它把一整类部署从"不可用"变成可用，收益最大。
3. **第 5 项（host mapping）第三**——两个字段、语义由 Chromium 定义并已有单测，
   能立刻关闭 `ERR_NAME_NOT_RESOLVED` 不可测这个盲区，也是 CDN 直连的前置条件。
4. **第 1 项（cookie）第四**——改动最大（删绑定层一整套补偿逻辑），
   但每删一处都有实测数字作对照，回滚边界清楚。
5. **第 4 项（DNS）第五**——纯新增字段，但 nameserver 列表与 DoH 模板的校验失败面更大。
6. **第 7 节（原生能力补齐）**——四项性质不同，内部再排序：
   - **7.1 HSTS 先做**：关闭一个可观测协议差异（明文先发），两个字段、语义完全由
     Chromium 定义，且预置列表已经编进产物。
   - **7.2 Cookie 语义第二**：不加策略开关，只在 Core 内注入 Chrome 默认 delegate；
     这是**行为变更**，需要有对照用例与 release note。
   - **7.4 代理链第三**：单跳够用的场景可以先不做。
   - **7.3 H2 params 最后**：它要求重新跑 72 个 profile 的第 2、3 级证据，是这一节里
     唯一有取证成本的项。
7. **第 8 项（平台身份）**——一个枚举、Core 侧只改一处 `#if` 分支；但它顺带补齐一个
   现在完全缺失的门禁（UA / platform / mobile 三者一致性），所以值得早做。
   其中 8.5（默认 UA 不再用库名）是**破坏性变更**，需要 release note。
8. **第 3 项（client hints）最后**——阻塞在 72 份 profile 证据上，不阻塞 v9 发布。

风险清单：

- **第 1 项的锁序**：cookie 函数与 `Request::StartOnNetworkThread` 共享 engine 状态，
  锁序写错会在 Chromium 网络线程上死锁。必须先写一个只在两线程间交替调用的
  `--gtest_repeat` 压测再合。
- **`mn_cookie_t` 的指针有效期**：与 v8 现有回调约定一致（只在回调期间有效），
  但 `cookies_get` 是同步返回、填充调用方 buffer，语义不同，必须在头文件注释里
  写清楚是"填充 caller 提供的数组"而不是"借用指针"。
- **`kUNKNOWN` 的诚实性**：如果实现时图省事把 `kUNKNOWN` 报成 `HTTP/1.1`，
  本提案就白做了。用例 2 的第二段是专门的守卫。
- **第 6 项的私钥内存驻留与 session cache**：`WrapOpenSSLPrivateKey` 把密钥留在进程内存，
  没有 OS keystore 保护；`SetClientCertificate` 会 `FlushForServers`，但绑定层换证书时
  仍必须自己丢掉该 server 的 Engine，否则新证书不生效。验收用例 3 专门守这条。
- **第 10 项是行为变更**：现在传 `Connection: close` 的代码会开始收到异常。这是对的——
  它从来没生效过，但下游会感知到，需要 release note。
- **第 9 项引入第四个 `net/` 层补丁**：`sync-core.sh` 已有 marker 校验与部分应用拒绝，
  但升级 Chromium 时这段代码若变动会冲突。升级流程必须把「复查 4 个补丁」列为一步。
- **第 9 项让非幂等请求从「可能自动恢复」变成「明确失败」**：这是有意的取舍，
  但必须进 release note，否则会被当成回归。
- **第 8.5 节改默认 UA 是破坏性变更**：`impersonate=None` 的 UA 会从 `minicronet/0.2.9`
  变成 Chrome 版本串。任何依赖旧值的测试或下游都要改；release note 必须写明，
  并指出 `user_agent=` 仍是逃生口。
- **第 8 节与 profile 冲突检测的交互**：`minicronet.cc:361-363` 在 `!profile.is_current()` 时
  对不匹配的 `user_agent` 返回 `MN_ERROR_PROFILE_CONFLICT`。`platform_override` 会让 UA
  与 profile 记录不同，必须把「平台不同但版本相同」放行，否则这项一开就撞上既有检查。
- **第 7.2 节是行为变更**：注入 Chrome 默认 delegate 会改变已有程序收到的 cookie 集合
  （SameSite=Lax 的 cookie 不再随跨站请求发出）。这必须进 release note，并给出对照用例，
  不能用"修了个 bug"的措辞带过。
- **第 7.3 节有取证成本**：H2 session params 纳入 profile 后，72 个 profile 的第 2、3 级
  证据要重跑。第 1 级（源码）本来就该覆盖这些字段。
- **第 2.5 节的诚实性**：桌面端 `EchMode` 恒为 `kDisabled`，所以 `ech_status` 会一直是
  `NOT_ATTEMPTED`。文档必须写清这一点，否则这个字段看起来像"ECH 可用"的承诺。
- **第 5 项的规则语法不能自己解析**：`MAP` / `EXCLUDE` / `^NOTFOUND` 的解析必须交给
  `HostMappingRules`，绑定层只转发字符串。自己写一个解析器就是第二份需要与 Chromium
  同步的实现，正是 §1 禁止的。

---

## 14. 与现有 v8 缺陷的关系

上一轮 review 剩下的三项 Major 已在 0.2.9 里单独修完，不需要等 v9：

| 残留 | 状态 |
| --- | --- |
| `headers={"User-Agent": None}` 无法删除 Core 注入的头 | **已修** —— 改为抛 `UnsupportedFeature`，覆盖仍然有效 |
| `tools/audit-python-stubs.py` 硬编码 `AUDITED` | **已修** —— 改为从 `IMPL.glob("*.py")` 推导，暴露并补齐了 `browser_headers` / `client_hints` 两个缺存根的模块 |
| `bindings/python36` 门禁空壳 | **已修** —— 换成 `tools/audit-python36-artifact.py`，检查产物而非零测试的 crate |

v9 关闭的是另一组：

| 项 | 关闭什么 |
| --- | --- |
| 2 协商协议 | `http_version` 恒为 `None` |
| 2.5 ECH 状态 | ECH 结果不可见，分不清"服务器不支持"与"平台关闭" |
| 6 mTLS | 双向认证这一整类部署完全不可用 || 7.1 HSTS | HSTS 站点先发明文 `http://`，与 Chrome 不一致 |
| 7.2 Cookie 语义 | SameSite 默认落桶与 Chrome 不同（**行为变更**） |
| 7.3 H2 params | END_STREAM / SETTINGS GREASE 可能与 Chrome 不同（走 profile 数据） |
| 8 平台身份 | 传参 UA 与 client hints 可互相矛盾；默认 UA 是库名 |
| 9 非幂等重发 | POST 在复用连接上被静默重发两次（**语义缺陷**，需要 Chromium 补丁） |
| 10 hop-by-hop 头 | `Connection: close` 发出去但不关闭连接；h2/h3 上被静默丢弃（**语义缺陷**，纯绑定层） |
| 5 Host mapping | CDN 直连不可表达；`ERR_NAME_NOT_RESOLVED` 在本机不可测 |
| 1 Cookie 存取 | 调用方改 jar 触发换引擎（实测 20 次编辑 → `builds=20`） |
| 3 Client hints | `Accept-CH` 协商不生效 |
| 4 DNS 配置 | 无 —— 纯新功能 |

其中三项"既是新能力、又当场关闭一个现存问题"：第 2.5 项把 ECH 从静默变成可断言，
第 5 项让一个不可测的错误映射变成可测，第 6 项让一整类部署从不可用变成可用。
这也是它们在落地顺序里占据前半段的原因。
