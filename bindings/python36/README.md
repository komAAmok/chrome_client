# chrome_client

[![PyPI 版本](https://img.shields.io/pypi/v/chrome-client)](https://pypi.org/project/chrome-client/)
[![Python 版本](https://img.shields.io/badge/python-3.6%20%7C%203.7%20%7C%203.8%20%7C%203.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14%20%7C%203.15-blue)](https://pypi.org/project/chrome-client/)
[![许可证：MIT](https://img.shields.io/pypi/l/chrome-client)](https://pypi.org/project/chrome-client/)

**Chromium 驱动的 HTTP/HTTPS 与 WebSocket 指纹客户端。**

它不是一个"带相似指纹"的客户端，而是把真实 Chromium 的网络栈编译成
`libminicronet`，直接由它发出请求。因此 TLS 的 ClientHello、HTTP/2 SETTINGS、
QUIC 传输参数、请求头顺序都不是模仿出来的，而是那一版 Chrome 自己的行为。

```python
from chrome_client import Session

with Session(impersonate="chrome_155") as session:
    response = session.get("https://example.com")
```

Python API 同时对齐两套习惯：`requests` 的 `Session`/`Response`/异常层次，以及
`curl_cffi` 的 `impersonate`、`http_version`、`AsyncSession`、`CurlMime` 和
WebSocket。同步与异步两套 API 共用同一个引擎，行为只在"怎么等待"上不同。

**无法用 Chromium 忠实实现的选项一律抛错**，不会静默忽略——静默降级比缺失更糟，
因为它让调用方以为自己在模仿一个指纹，实际上不是。细则见
[兼容边界](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)。

[English README](https://github.com/komAAmok/chrome_client/blob/main/README.en.md) · [构建说明](https://github.com/komAAmok/chrome_client/blob/main/docs/BUILD.md) · [兼容边界](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md) · [第三方许可](https://github.com/komAAmok/chrome_client/blob/main/THIRD_PARTY_NOTICES.md)

## 支持范围

### 操作系统、架构与绑定

| 操作系统 | Core 目标 | Python 3.9–3.15 | Python 3.6–3.8 | Rust | 备注 |
| --- | --- | --- | --- | --- | --- |
| Linux | x86 (`i686`)、x86_64、ARM64 | ✓ | ✓（独立 abi3 扩展） | ✓ | manylinux/glibc 运行时依赖见 manifest |
| Linux | ARMv7 (hard-float, NEON) | 未发布 | 未发布 | ✓ | Core 已构建并审计；不发布 wheel，需自行构建扩展 |
| Windows | x86、x86_64、ARM64 | ✓ | x86/x86_64 ✓ | ✓ | DLL 随 wheel；ICU 数据已编入库中 |
| macOS | x86_64、ARM64 | ✓ | ✓（独立 abi3 扩展） | ✓ | dylib 按架构匹配 |


每个 Core 产物位于 `core/binaries/<target>/`，带 ABI 版本、Chromium revision、
SHA-256 和依赖清单。ABI 当前为 v12。Go 和 Node.js 目录目前是绑定设计说明，
不是已发布的可安装包。

### Chrome profile

`impersonate` 必须给出精确的 `chrome_<major>` 名称（同时接受 curl-cffi 风格的
`chrome<major>` 别名）。**不接受**裸的 `chrome`/`chromium`：那会解析到本构建恰好
最后收录的 profile，一旦新增 profile，调用方的指纹就会在无人要求的情况下改变，
因此抛 `ImpersonateError` 并要求写明确切版本。当前支持
Chrome 86–157，共 72 个 profile；范围外的版本抛 `ImpersonateError`，不会静默降级。
`available_profiles()` 返回完整列表。Edge、Safari、Firefox、Tor 等非 Chromium 目标
同样显式报错，而不是当作 Chrome 处理。

| Chrome 主版本 | 可用 profile |
| --- | --- |
| 86–92 | `chrome_86` … `chrome_92`（各自 tag 独立审计；无 ALPS 扩展，H3 基准缺失，见 `docs/CHROME_86_98_SUPPORT.md`） |
| 93–98 | `chrome_93` … `chrome_98` |
| 99–105 | `chrome_99` … `chrome_105` |
| 106–112 | `chrome_106` … `chrome_112` |
| 113–119 | `chrome_113` … `chrome_119` |
| 120–126 | `chrome_120` … `chrome_126` |
| 127–133 | `chrome_127` … `chrome_133` |
| 134–140 | `chrome_134` … `chrome_140` |
| 141–147 | `chrome_141` … `chrome_147` |
| 148–157 | `chrome_148` … `chrome_157`（`chrome_154`–`chrome_157` 用基于 156 内核的 Core，指纹分别按各自 tag 审计） |

Profile 影响 TLS ClientHello、ALPN、HTTP/2 设置、QUIC/H3 和相关 Chromium 网络
参数；它不是完整 Chrome 浏览器，也不包含 Blink、扩展、Service Worker 或持久化
浏览器 Profile。

### 功能矩阵

| 功能 | Python | Rust/Core | 说明 |
| --- | --- | --- | --- |
| HTTP/1.1、HTTP/2、HTTP/3/QUIC | ✓ | ✓ | 默认由 Chromium 协商；`http_version="v1"/"v2"/"v3"` 可强制 |
| HTTPS/TLS、证书校验 | ✓ | ✓ | `verify=False` 或 `verify="/path/ca.pem"` 自定义 CA；证书失败按具体检查项报错 |
| mTLS 客户端证书 | ✓ | ✓ | `cert=("c.pem","k.pem")` 或单个合并 PEM；服务端要求时自动出示，不改变 ClientHello |
| 域名映射到指定 IP | ✓ | ✓ | `host_mapping="MAP cdn.test 93.184.216.34"`；**只改写解析结果**，SNI、Host 头、证书校验仍用域名 |
| DNS 服务器 / DoH（DoT 不支持，抛错） | ✓ | ✓ | `dns_nameservers="1.1.1.1"`、`doh_template=...` 需配 `dns_secure_mode=1/2`、`dns_rotate`（`dns_padding` 不支持，抛错）。实测：指向死服务器的 `dns_nameservers` 仍返回 200（Chromium 的 SYSTEM 兜底）；`dns_secure_mode=2` 的 DoH 不回退 |
| ECH（Encrypted ClientHello） | ✓ GREASE | ✓ | `Response.ech_status` 报告 `success` / `rejected` / `not_attempted`。**版本边界：`chrome_118` 及以前不发 ECH 扩展，`chrome_119` 起发 GREASE**（payload 长度在 372/436/500/564 间随机，正是 GREASE 的特征）。桌面 Chromium 只从 DNS HTTPS 记录取 ECHConfigList，本引擎不取，所以真实配置永远不出现，`ech_status` 恒为 `not_attempted`。逐版本证据见 `docs/ABI.md` 末节 |
| 平台身份 | ✓ | ✓ | `platform="windows"/"macos"/"linux"`；UA 组、`sec-ch-ua-platform`、`sec-ch-ua-mobile` 同源，Linux 主机可发 Windows UA |
| 每 origin 连接上限 | ✓ | ✓ | `max_sockets_per_group=32`；默认 0 保留 Chromium 自身的 6，提高是对被模仿浏览器的可观测偏离 |
| HSTS 例外与多跳代理 | ✓ | ✓ | `hsts_bypass="example"`（只接受单标签）、`proxy_chain="socks5://a:1080,http://b:8080"` |
| HTTP/HTTPS/SOCKS 代理 | ✓ | ✓ | `proxy`、Requests 风格 `proxies`，运行期可改 |
| 同步请求 | ✓ | ✓ | 同步等待释放 GIL，可直接放进线程池 |
| asyncio 请求 | ✓ | — | Core 回调唤醒事件循环，无请求线程池；每次唤醒批量取事件 |
| 流式响应 | `iter_content` / `aiter_content` / `raw` | `ResponseStream` | 每请求 body 队列上限 1 MiB；达到上限时向 Core 返回 `MN_READ_PAUSE`（ABI v11）让其停止读 socket，消费者取走后 `resume_read` 恢复 |
| 分块上传 | `data=` 传文件对象或迭代器 | `Upload::Chunked` | 同步与异步都走 `upload_write` |
| 取消、超时、大小限制 | ✓ | ✓ | `Timeout`、`ResponseTooLarge`、`RequestException` |
| WebSocket / WSS | ✓ | ✓ | 同步与异步，curl-cffi 方法名齐全 |
| Cookie | ✓ | ✓ | `session.cookies` 为 `RequestsCookieJar`，读写都生效 |
| 重定向 | ✓ | ✓ | `history`、最终 URL、`max_redirects`、`allow_redirects=False` |
| 内存缓存 | ✓ | ✓ | 随 Engine 生命周期存在，`cache=False` 可关闭 |


## 安装与加载 Core

```bash
python -m pip install chrome-client
```

从源码使用时，先针对某个 Core 目录构建扩展（`MINICRONET_CORE_DIR` 在构建期读取），
运行时把同一目录放到加载路径上：

```bash
MINICRONET_CORE_DIR=$PWD/core/binaries/linux-x86_64 cargo build --release -p chrome-client-python

LD_LIBRARY_PATH=$PWD/core/binaries/linux-x86_64 PYTHONPATH=bindings/python python -c \
  'import chrome_client; print(chrome_client.get("https://example.com").status_code)'
```

### 类型提示

包里带 `py.typed` 和完整的 `.pyi` 存根（`bindings/python/chrome_client/**/*.pyi`），
所以不需要额外的 stub 包：IDE 直接补全每个参数、识别返回值，并在悬停里显示
Args/Returns/示例。运行时行为不变——存根不参与导入，`typing_extensions` 只是类型检查期依赖。

```python
from chrome_client import Session

with Session(impersonate="chrome_155") as session:
    session.get(url, timeout=(5, 15))
    # 悬停 impersonate= 会列出 chrome_86 … chrome_157，以及 curl-cffi 的
    # chrome<major> 别名；裸 chrome/chromium 会被拒绝
    # session.get(...) 会补全 headers/cookies/data/json/timeout/proxies/verify/...
```

`impersonate` 与 `http_version` 用 `Literal` 给出了全部可选值；请求方法的全部选项
（`headers`、`cookies`、`data`、`json`、`timeout`、`proxies`、`verify`、`auth` 等）
都写成具名参数而不是藏在 `**kwargs` 里，PyCharm 等任何 IDE 都能补全，
`impersonate="chrome_200"` 或拼错的选项在写代码时就会被标出，而不是等到运行期。覆盖范围、存根与实现的校验方式见
[类型提示说明](https://github.com/komAAmok/chrome_client/blob/main/docs/PYTHON_TYPING.md)。

## Python 使用示例

### requests 风格

```python
import chrome_client as requests          # 或 from chrome_client import requests

response = requests.get(
    "https://example.com/api",
    params={"page": 1},
    headers={"Accept": "application/json"},
    impersonate="chrome_155",
    timeout=15,
)
response.raise_for_status()
print(response.status_code, response.reason, response.json())
```

`Session` 与 `requests.Session` 同形：`headers`、`params`、`cookies`、`proxies`、
`auth`、`hooks`、`stream`、`verify`、`max_redirects`、`trust_env`、`adapters`、
`mount()`、`prepare_request()`、`send()`、`resolve_redirects()`、`close()` 和
上下文管理器都可用。

```python
from chrome_client import Session

with Session() as session:
    session.headers.update({"Accept": "application/json"})
    session.get("https://example.com/login")          # 服务端 Set-Cookie
    print(session.cookies.get_dict())                  # 会话内可见
    session.cookies.set("consent", "1", domain="example.com", path="/")
    session.get("https://example.com/private")         # 自动带上两类 cookie
```

### 会话保持：cookie 与代理

`session.cookies` 是 `RequestsCookieJar`（`http.cookiejar.CookieJar` 子类），
带 domain/path/secure 元数据，读写都会生效：

```python
session.cookies.get_dict()
session.cookies.get_dict(domain="example.com")
session.cookies.set("sid", "abc", domain="example.com", path="/")
session.cookies.update({"a": "1"})
del session.cookies["sid"]
session.cookies.clear()
```

响应侧 cookie 由 Core 内的 Chromium `CookieMonster` 拥有并自动附加；facade 会把每
一跳（含重定向）的 `Set-Cookie` 同步进 `session.cookies` 和 `response.cookies`。
当调用方改动 jar 与 Core 已存的 cookie 冲突时，Core 会覆盖请求头，因此 facade 会
换一个空 cookie store 的同配置 Engine，让改动真正生效——连接池仍按配置复用。
`cookies={...}` 只作用于**这一次请求**：实测 `get(url, cookies={"per": "P"})` 之后
再发一个不带该参数的请求，不会带上 `per`（与 requests 一致）。同名覆盖在请求结束后
还原成会话里的值，而不是把会话值删掉。已知例外：`discard_cookies=True` 的 302 链，
第二跳仍会带上第一跳设下的 cookie——抑制需要 per-hop 开关，而 M156 没有
（`docs/TBD_VERIFICATION.md` T-1），`allow_redirects=False` 是保持承诺的写法。
同时传 `cookies=` 与 `discard_cookies=True` 会抛 `UnsupportedFeature`（不再静默丢弃）；
字面 `Cookie` 头则仍以 `discard_cookies` 为准，且不改写调用方传入的请求对象。

`session.proxies` 是普通可变映射，运行期改动立即生效：

```python
session.proxies.update({"https": "http://user:pass@127.0.0.1:8080"})
session.get("https://example.com")                     # 走代理
session.proxies.clear()
session.get("https://example.com")                     # 直连
session.get("https://example.com", proxy="socks5://127.0.0.1:1080")   # 单次覆盖
```

`proxies` 按 `scheme://host`、`scheme`、`all://host`、`all` 顺序匹配；显式
`proxy=` 优先。`trust_env=True`（默认）时读取 `HTTP_PROXY`/`HTTPS_PROXY`/
`NO_PROXY`。代理是 Engine 级设置，切换代理会换一个 Engine，但 jar 里的 cookie 会
继续随请求发出。

**代理池要把 `max_engines` 设到不小于池大小。** 一个 Engine 是一整个 Chromium
`URLRequestContext`（独立线程、socket 池、DNS 缓存、TLS 会话缓存），一个会话默认
只保留 8 个（`max_engines=8`），超出按 LRU 淘汰。池比它大时，每个请求都会重建上下文
并新开一条 TCP 连接，而不是复用——20 个代理跑 200 个请求的实测：

| `max_engines` | 构建的 Chromium 上下文 | 代理侧 TCP 连接 | 遗留 TIME_WAIT |
| --- | --- | --- | --- |
| 8（默认） | 200 | 200 | +180 |
| 20 | 20 | 20 | +20 |

```python
session = chrome_client.Session(max_engines=32)   # 池有 32 个端点
```

不必靠 TIME_WAIT 才发现这件事：`Session.stats` 直接给出计数。

```python
with chrome_client.Session(impersonate="chrome_151") as session:
    session.get(url)
    session.get(url)
    print(session.stats)
    # {'live': 1, 'hits': 1, 'builds': 1, 'evictions': 0, 'deferred': 0,
    #  'inflight': 0, 'generation': 0, 'max_engines': 8}
```

`builds` 一直涨就是每次请求都在换传输上下文；`hits` 才代表连接、TLS 会话
（session resumption）和 HTTP/2/3 会话池被真正复用。

`Session.binding(url)` 返回这条请求将要使用的传输上下文本身——profile（JA3/JA4、
H2 SETTINGS 与伪头部、QUIC 传输参数都由它冻结）、代理、TLS 校验策略。两个
`binding` 相等的请求共享同一个 `URLRequestContext`：

```python
with chrome_client.Session(impersonate="chrome_151", proxy="http://p:8080") as session:
    print(session.binding("https://example.com"))
    # SessionBinding(impersonate='chrome_151', proxy='http://p:8080')
```

代理规则**优先走 Chromium 原生语法**，只在原生不支持时才扩展。原生可解析的三种
形式会原样交给 Core（此前 facade 会把它们重写成单个 `scheme://host`，直接弄坏）：

| 原生形式 | 含义 | 之前 |
| --- | --- | --- |
| `http=h:p` | 只给 HTTP 用这个代理 | 被改写成 `http://http=h:p` |
| `http=h:p;https=h:p` | 按目标 scheme 分别指定 | 被改写 |
| `http://a:p,http://b:p` | 回退列表，按顺序试 | 被拒绝 |
| `direct://` | 不走代理 | 被当成未知 scheme 拒绝 |

这些只做校验（规则键、`direct://` 位置、内嵌凭据）后**原字节透传**。其余都是
**扩展层**，只在 Chromium 直接拒绝该输入时才存在：

| 写法 | 处理 | 原因 |
| --- | --- | --- |
| `http://` `https://` `socks://` `socks4://` `socks5://` | 原样使用 | Chromium 原生白名单 |
| `socks5h://` | 改写为 `socks5://` | 同名同事，Chromium 只是少了这个拼写 |
| `socks4a://` | `UnsupportedFeature` | 要求远端解析，Core 没有 |
| `quic://` | `UnsupportedFeature` | 本构建未开 `ENABLE_QUIC_PROXY_SUPPORT` |
| `host:port`（无 scheme） | `http://host:port` | Chromium 把裸规则默认成 HTTP |
| 尾部 `/`、路径、`#片段`、换行、空白 | 剥掉 | 规则只有 host:port |
| `host:`（空端口） | `InvalidProxyURL` | Chromium 同样拒绝 |
| `host:0` | 保留端口 0 | `ParsePort("0")` 是真端口，不是默认值 |
| 权限部分后面的多余文本 / 未知 scheme / `?query` / 双 scheme | `InvalidProxyURL` | 无法解析 |

原生规则键是 `http`/`https`/`ftp`/`socks`；`socks=` 按 Chromium 语义默认 **SOCKS4**，
要 SOCKS5 请写 `socks=socks5://h:p`。**SOCKS5 的账号密码认证不支持**
（Chromium 只提供 `0x00` 无认证方式），需要认证请用 HTTP/HTTPS 代理。细节见
[docs/COMPATIBILITY_BOUNDARY.md](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)。

### 重定向、异常与响应

```python
response = session.get("https://example.com/r")
print(response.url, response.history, response.redirect_count)
response = session.get("https://example.com/r", allow_redirects=False)
print(response.status_code, response.headers["Location"], response.next.url)
session.get("https://example.com/r", allow_redirects=False)  # 拿到 3xx 本身
```

`max_redirects` 只能是 `None`（= Chromium 在一个 `URLRequest` 内的上限 20）：数值一律抛
`UnsupportedFeature`，因为它无法下调该上限，而在 Python 侧重发会另写一套重定向语义；
大于等于 20 抛 `UnsupportedFeature`，不静默钳到 20。默认值是 `None`，含义是
「采用 Chromium 的 20」，**不是** requests 的 30——`requests.models.DEFAULT_REDIRECT_LIMIT`
仍是 30 且仍可导入，但本包不把它当作默认值。

异常层次与 `requests.exceptions` 一致（`RequestException` 继承 `IOError`），
并补上 curl-cffi 的叶子类型。Chromium 的 net error 会映射成具体类型和可读名字：

```python
try:
    session.get("https://expired.example.com")
except chrome_client.CertificateVerifyError as error:
    print(error)          # ERR_CERT_DATE_INVALID (net error -201)
except chrome_client.ConnectionError:
    ...
except chrome_client.Timeout:
    ...
```

证书失败按具体检查项区分：过期 `ERR_CERT_DATE_INVALID (-201)`、主机名不匹配
`ERR_CERT_COMMON_NAME_INVALID (-200)`、CA 不受信 `ERR_CERT_AUTHORITY_INVALID (-202)`，
类型均为 `CertificateVerifyError`（继承 `SSLError` → `ConnectionError`）。要接受私有
CA 用 `verify="/path/ca.pem"`，完全跳过校验用 `verify=False`（Engine 级设置）。

`Response` 提供 `status_code`、`reason`、`headers`、`cookies`、`history`、
`elapsed`、`request`、`url`、`encoding`、`apparent_encoding`、`text`、`content`、
`json()`、`raw`、`links`、`is_redirect`、`is_permanent_redirect`、`next`、`ok`、
`raise_for_status()`、`iter_content()`、`iter_lines()`、`close()`，以及 curl-cffi
的 `http_version`、`charset`、`redirect_count`、`redirect_url`。

### curl-cffi 风格：指纹与协议

```python
from chrome_client import Session, AsyncSession, CurlMime

with Session(impersonate="chrome152", http_version="v2") as session:
    session.get("https://example.com")
```

`impersonate` 接受 `chrome_155`、`chrome155`，以及解析到最新 pinned 版本的
`chrome`。TLS ClientHello、ALPN、HTTP/2 设置与优先级、HTTP/3 传输参数全部来自该
profile：这也是 `ja3=`、`akamai=`、`perk=` 和大部分 `extra_fp` 字段会抛
`UnsupportedFeature` 的原因——接受一个 JA3 字符串却仍然发送 Chromium 自己的
ClientHello，等于谎报保真度。`extra_fp` 中 facade 能真正实现的两项会被采纳：
`header_order` 和 `form_boundary`。

**`http_version=v1` 的 ALPN 与 ALPS 是有意的差异，不是指纹缺陷。** 强制 HTTP/1.1 时
ALPN 只发 `http/1.1`；ALPS（扩展 17613）也随之为空——Chromium 只对 `alpn_protos`
里存在的协议声明 ALPS，而 `application_settings` 表只装了 h2 的条目
（`net/socket/ssl_client_socket_impl.cc` 的 `alpn_protos` 循环）。真实 Chrome 走同一路径，
基准里的 h1 抓包同样没有该扩展。因此用 peetprint 对照时，**`v1` 要对 h1 抓包，
normal/H2 才对 h2 抓包**；拿 `v1` 去对 h2 抓包必然只报这两处。`trust_anchors`（0xca34）
与协议无关，h1 下照常发送，不要把它一并当成"h1 本该没有"。
门禁见 `tools/audit-h1-alps-absence.py`。

**默认请求头来自 profile 自己的抓包。** `impersonate="chrome_155"` 发出的头集合，
与那个版本的 Chrome 在 `profiles/chrome_155/h1/captures.json` 里留下的六条抓包逐字节
一致——顺序也一致，包含 `sec-ch-ua`、`sec-ch-ua-mobile`、`sec-ch-ua-platform`、
`Upgrade-Insecure-Requests`、`Accept`、四个 `Sec-Fetch-*`、`Accept-Encoding`、
`Accept-Language`。72 个 profile 全部如此，`bindings/python/tests/test_client_hints.py`
逐个比对，`tools/audit-client-hints.py` 在提交前重新从抓包推导整张表。

**这些头现在由 Core 自己发。** ABI v9 把这 3 组不同的头集合（9 / 11 / 12 个字段，
按 profile 去重后共 39 KB）编进了 `libminicronet`，由
`Request::StartOnNetworkThread` 经 `SetExtraRequestHeaders` 送出——与 Chrome 在
`services/network` 的 URLLoader 工厂层生成它们的位置相同，而不是在绑定层拼好再塞进去。
`mn_engine_config_t.inject_client_hints` 可以关掉它（默认 1）。

为什么这一层重要：`Accept-CH` 协商看到的是 `net` 层组装出的请求；facade 注入时这些头
停在 extra headers 里，`net` 自己不知道它们存在。

**平台跟随宿主，不跟随抓包。** 抓包来自 Windows 桌面，但客户端不能声称一个
套接字并不在的平台。`User-Agent` 的括号组与 `sec-ch-ua-platform` 都按宿主重写，
与 Core 自己的推导（`core/source/minicronet.cc` 的 `BUILDFLAG(IS_WIN/IS_MAC)` 分支）
保持一致——`"Windows"` 的 hint 配 `X11; Linux` 的 UA 比缺一个 hint 更容易被检出。

**`Sec-Fetch-*` 描述的是顶层导航。** 每条抓包都是一次导航，因为浏览器打开 URL 就是
导航，而 `session.get(url)` 是同一件事。要子资源语义就自己传那两个字段，其余字段
仍然保留：

```python
session.get(url, headers={"Sec-Fetch-Site": "same-origin",
                          "Sec-Fetch-Mode": "no-cors",
                          "Sec-Fetch-Dest": "image"})
session.get(url, headers={"Sec-Fetch-User": None})   # 删掉单个注入的字段
session.get(url, default_headers=False)              # 整个注入关掉
```

`impersonate=None` 时**一个都不注入**：没有声称自己是浏览器的会话不该看起来像 Chrome。

`Priority` 是这条规则的例外：RFC 9218 的 `priority` 请求头由 `net` 层自己序列化
（`net/spdy/spdy_http_utils.cc` 的 `CreateSpdyHeadersFromHttpRequest`），不经
URLLoader。因此 chrome_124 起的 profile 会自动带上它，取值由请求优先级决定——
这些 profile 的默认优先级是 `highest`，发出的正是真实 Chrome 导航的
`priority: u=0, i`；chrome_86–123 不发这个头，与它们的抓包一致。HTTP/1.1 上不发。

不受支持而显式报错的还有：`interface=`、`doh_url=`、
`curl_options=`、`max_recv_speed=`、`referer=`。`referer` 尤其容易误判：Chromium
自己拥有 referrer 并会剥掉调用方设置的 `Referer` 头，ABI v11 也没有 referrer 字段，
所以设置它在任何路径下都不会到达网络。

multipart 两种写法都支持：

```python
session.post(url, data={"title": "t"}, files={"f": ("a.txt", b"...", "text/plain")})

mime = CurlMime()
mime.addpart(name="title", data="hello")
mime.addpart(name="photo", filename="p.jpg", content_type="image/jpeg",
             local_path="/tmp/p.jpg")
session.post(url, multipart=mime)
```

### 流式读写与分块上传

```python
with session.stream("GET", "https://example.com/large") as response:
    for chunk in response.iter_content(64 * 1024):
        ...

response = session.get(url, stream=True, max_response_bytes=16 * 1024 * 1024)
try:
    for line in response.iter_lines():
        ...
finally:
    response.close()        # 未读完时取消 native 请求

with open("big.bin", "rb") as handle:
    session.post(url, data=handle)      # 文件对象或迭代器走分块上传
```

`max_response_bytes` 超限会取消请求并抛 `ResponseTooLarge`。每个请求的 body 队列
上限 1 MiB，超过时绑定层返回 `MN_READ_PAUSE`（ABI v11）让 Core 停止读 socket，
不会占住 Core 线程；实测见 `docs/PERF_TEST_AND_LEAK.md` §5.6。

### 并发

同步 `Session` 可直接跨线程共享：所有阻塞调用都释放 GIL，Engine 缓存有锁。

```python
from concurrent.futures import ThreadPoolExecutor

with Session() as session, ThreadPoolExecutor(max_workers=32) as pool:
    codes = list(pool.map(lambda url: session.get(url).status_code, urls))
```

asyncio 路径不创建线程池：Core 回调用 `call_soon_threadsafe` 唤醒事件循环，每次
唤醒批量取事件，因此一个几 MB 的响应不会按 chunk 逐次往返事件循环。

```python
import asyncio
from chrome_client import AsyncSession

async def main():
    async with AsyncSession(impersonate="chrome_155", max_clients=64) as session:
        responses = await asyncio.gather(*[session.get(u) for u in urls])
        async with session.stream("GET", big_url) as response:
            async for chunk in response.aiter_content(65536):
                ...

asyncio.run(main())
```

`max_clients` 限制**同时被准入的请求数**：`stream=False` 时它覆盖整个响应，
`stream=True` 时只覆盖到响应头，之后的 body 读取在门外（详见
[兼容边界](https://github.com/komAAmok/chrome_client/blob/main/docs/COMPATIBILITY_BOUNDARY.md)）。
进程池请使用 `spawn` 或 `forkserver`：Engine
存在后进程已是多线程且 Chromium 线程持锁，`fork()` 可能在子进程执行任何 Python
代码之前就死锁。

Chromium 对同一 host 组默认最多 6 条 HTTP/1.1 连接（实测同一 host 并发上限确为 6，
换用多个 host 后吞吐随之上升）。这是浏览器语义的一部分，不是绑定的瓶颈：需要更高
单 host 并发时应使用 HTTP/2 或 HTTP/3 端点。

### WebSocket / WSS

```python
from chrome_client import WebSocket

with WebSocket(url="wss://echo.example.com", impersonate="chrome_155") as socket:
    socket.send_str("ping")
    print(socket.recv_str())
```

```python
async def main():
    async with AsyncSession() as session:
        socket = await session.websocket("wss://echo.example.com")
        async with socket:
            await socket.send_json({"op": "ping"})
            print(await socket.recv_json())
```

同步与异步都提供 `send`/`send_str`/`send_bytes`/`send_json`/`ping`、
`recv`/`recv_str`/`recv_bytes`/`recv_json`/`recv_fragment`、`close`、`terminate`，
同步侧还有 `run_forever(on_message=..., on_error=..., on_open=..., on_close=...)`。
构造函数返回时握手已完成——Core 在 open 之前会拒绝 `send()` 和 `close()`。

握手头有两条 Chromium 规则：

- **UA 不能按次传。** Core 明确拒绝把 `User-Agent`、`Host`、`Origin`、
  `Connection`、`Upgrade`、`Sec-WebSocket-*` 作为额外头（`IsForbiddenWebSocketHeader`），
  因为 Chromium 自己决定这些头的取值和在握手里的位置，而 UA 的位置本身就是指纹的
  一部分。要改 WebSocket 的 UA，用 `Session(user_agent=...)`（Engine 级设置，HTTP 与
  WS 一致）或换 `impersonate` profile；传 header 会得到 `UnsupportedFeature`，而不是
  静默生成一个与其它请求 UA 不一致的握手。
- **`Origin` 默认取自 URL 自身。** Core 拒绝空 origin，而这里没有页面可继承，所以
  默认用 `ws://host:port` 对应的 `http://host:port`——即同源页面发起连接的样子。需要
  别的值就显式传 `origin=`。

Cookie 会按 URL 匹配后附加到握手上。

## 与 requests / curl-cffi 的对照

`from curl_cffi import requests` 改成 `from chrome_client import requests`，或
`import requests` 改成 `import chrome_client as requests`，多数代码不需要其他改动。

| 用法 | 支持情况 |
| --- | --- |
| `get/post/put/patch/delete/head/options/trace/query` | ✓ |
| `Session`、`AsyncSession`、`Client`、`AsyncClient` | ✓（`Client` 是 `Session` 别名） |
| `params`、`data`、`json`、`content`、`files`、`multipart` | ✓ |
| `headers`、`cookies`、`auth`、`timeout` | ✓ |
| 删除 Core 自有的头（`User-Agent`、`Accept-Encoding` 等） | ✗ 抛 `UnsupportedFeature`；覆盖仍然可以 |
| `dns_padding=True` | ✗ 抛 `UnsupportedFeature`；Chromium 没有任何 EDNS0 padding 控制，这个值曾经一路传到 `EngineOptions` 就断了（`core/source/engine.cc` 从不读它），调用方要求填充查询、实际发出未填充的，且没有任何提示 |
| `dns_over_tls=True` | ✗ 抛 `UnsupportedFeature`；Chromium 桌面版没有 DoT 客户端，设了只会禁用内置解析器（`net/dns/dns_client.cc:168`）并静默退回系统解析，`dns_nameservers`/`doh_template` 全部失效。加密解析请用 `doh_template` + `dns_secure_mode=2` |
| 设置 hop-by-hop 头（`Connection`、`Keep-Alive`、`TE`、`Trailer`、`Upgrade`、`Proxy-Connection`） | ✗ 抛 `UnsupportedFeature`；Chromium 拥有连接生命周期，h2/h3 上这些字段被直接丢弃 |
| `timeout=(connect, read)` 两值不同 | ✗ 抛 `UnsupportedFeature`（ABI v11 只有一个期限）；两值相同可用 |
| `proxies`、`proxy`、`proxy_auth`、`trust_env`、`NO_PROXY` | ✓ |
| `verify=True/False/"/path/ca.pem"` | ✓ |
| `host_mapping="MAP <域名> <IP>[:端口]"`、`EXCLUDE <域名>`、以及它们的列表 | ✓ 只改写解析结果，SNI/Host/证书校验仍是域名；写错关键字抛 `UnsupportedFeature` |
| `dns_nameservers`、`doh_template`、`dns_secure_mode`、`dns_rotate` | ✓ 任一非空即改由 Chromium 自带解析器处理；`doh_template` 缺 `dns_secure_mode` 抛错而不是被忽略。**实测**：`dns_nameservers` 指向本地 53 端口时服务端真的收到 A(1) 与 HTTPS(65) 两类查询；`doh_template` 指向 `127.0.0.1:1` 在 `dns_secure_mode=2` 下失败、在 `=1` 下回退到系统解析成功——这正是 secure 与 opportunistic 的区别 |
| `platform`、`max_sockets_per_group`、`hsts_bypass`、`proxy_chain` | ✓ 都是引擎级、进 engine 缓存键；`platform` 同时决定 UA 组与 client hints |
| `cert=("c.pem","k.pem")`、`cert="combined.pem"`、`set_client_certificate()` | ✓ 服务端发 CertificateRequest 时才出示，不影响 ClientHello 指纹 |
| `allow_redirects`、`max_redirects`、`history`、最终 URL | ✓ |
| `stream=True`、`iter_content`、`iter_lines`、`raw` | ✓ |
| `aiter_content`、`aiter_lines`、`atext`、`acontent`、`session.stream(...)` | ✓ |
| `hooks={"response": ...}`、`prepare_request`、`send`、`mount`、自定义 adapter | ✓ |
| `Request`、`PreparedRequest`、`codes`、`CaseInsensitiveDict`、`Headers`、`Cookies` | ✓ |
| `HTTPBasicAuth`、`HTTPProxyAuth`、`AuthBase` | ✓ |
| `requests.exceptions` 全层次 + curl-cffi 叶子类型 | ✓ |
| `impersonate`、`http_version`、`retry`/`RetryStrategy`、`raise_for_status=True` | ✓ |
| `base_url`、`discard_cookies`、`default_encoding`、`content_callback` | ✓ |
| `CurlMime`、`ExtraFingerprints(header_order=..., form_boundary=...)` | ✓ |
| `HTTPDigestAuth` | 部分：需要用 `parse_challenge()`/`handle_401()` 显式驱动 |
| `HTTPAdapter(pool_connections=..., pool_maxsize=...)` | 接受但不生效：连接池由 Chromium 拥有 |
| `ja3`、`akamai`、`perk`、`extra_fp` 的 TLS/HTTP2 字段 | ✗ 显式抛 `UnsupportedFeature` |
| `interface`、`doh_url`、`curl_options`、`max_recv_speed` | ✗ 显式抛 `UnsupportedFeature` |
| `referer=` / `Referer` 头 | ✗ 显式抛 `UnsupportedFeature`（Chromium 会剥掉） |
| 持久化 cookie/cache 文件、`Curl` 低层句柄、`CurlOpt`/`CurlInfo` | ✗ |

行为差异（有意为之，不是缺陷）：

- 模块级 `chrome_client.get(...)` 共用一个进程内 `Session`，因此也共用连接池和
  cookie store。requests 每次新建 `Session`；这里一个 Session 等于一整个 Chromium
  `URLRequestContext`，按次新建会付出线程与内存代价。需要隔离时显式建 `Session`，
  或调用 `close_shared_session()`。
- `session.headers` 默认为空。requests 会预置 `User-Agent`/`Accept`/
  `Accept-Encoding`/`Connection`，而这里 Chromium 只自己发 `Host`/`Connection`/
  `User-Agent`/`Accept-Encoding`；`Accept` 两者都不发，注入 `Accept: */*` 反而会被
  指纹检测看见（真实 Chrome 是发的，见上文的 client hints 说明）。
- `Response.raw` 是覆盖 `read`/`stream`/`close` 的文件对象，不是 urllib3
  `HTTPResponse`。
- `Response.ok` 用 requests 语义（`status_code < 400`），不是 curl-cffi 的
  200–399。

### Core 体积与目录

Core 体积在 6.1–9.4 MB 之间（Linux ARMv7 最小，Windows x86_64 最大，静态 MSVC/UCRT
多出约 2 MB）。IDNA-only 的 ICU 数据已编入库中，所以不需要外挂 `icudtl.dat`；用不到的
磁盘缓存后端不进链接产物。各平台的体积上限由 `tools/audit-core-*.sh` 把关，超了直接
构建失败。

| Core 目录 | Rust target | Core 文件 |
| --- | --- | --- |
| `linux-x86` | `i686-unknown-linux-gnu` | `libminicronet.so` |
| `linux-x86_64` | `x86_64-unknown-linux-gnu` | `libminicronet.so` |
| `linux-armv7` | `armv7-unknown-linux-gnueabihf` | `libminicronet.so` |
| `linux-arm64` | `aarch64-unknown-linux-gnu` | `libminicronet.so` |
| `windows-x86` | `i686-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-x86_64` | `x86_64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `windows-arm64` | `aarch64-pc-windows-msvc` | `minicronet.dll` + `minicronet.lib` |
| `macos-x86_64` | `x86_64-apple-darwin` | `libminicronet.dylib` |
| `macos-arm64` | `aarch64-apple-darwin` | `libminicronet.dylib` |


## 目录与验证

| 路径 | 内容 |
| --- | --- |
| `core/abi/` | 稳定 C ABI v12 |
| `core/binaries/` | 9 个已审计平台 Core |
| `crates/minicronet/` | Rust 安全层、流和生命周期 |
| `bindings/python/` | Python 3.9–3.15 绑定与共享 facade |
| `bindings/python36/` | Python 3.6–3.8 独立 abi3 绑定（共用同一 facade） |
| `bindings/python/tests/` | `test_stability.py` 生命周期与并发；`test_compat.py` requests/curl-cffi 兼容面 |
| `docs/` | 构建、平台、ABI、兼容性和审计说明 |

回归测试需要已审计的 Core：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  python -m unittest discover -s bindings/python/tests -p "test_*.py"
```
