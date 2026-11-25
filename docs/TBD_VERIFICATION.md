# §8 待验证清单：六条的实测结论

审计报告 §8 列了六条「待验证」。逐条跑过之后：**三条结论改写了原来的判断，两条证实，一条发现我给出的验证命令本身就是错的**。

每条都给出实际执行的命令与原始输出，可重跑。

环境：Chromium 浅克隆 `$CHROMIUM_SRC`（`58bcc148f76`，只有 main 的浅历史、无 tag；路径取自 `tools/core-paths.sh`），网络可用（`chromium.googlesource.com` 返回 200），无 Rust 工具链。

---

## T-1 ｜ `LOAD_DO_NOT_SEND_COOKIES` 是否存在 —— **结论改写**

### 原判断

「`load_flags_list.h` 只有 `DO_NOT_SAVE_COOKIES`，但 `url_request.h:910` 的注释引用了它」——当时只能记为「待验证」。

### 实测

```bash
grep -rn 'DO_NOT_SEND_COOKIES' "$CHROMIUM_SRC/net/" --include=*.h --include=*.cc
```

输出只有两处**注释**：

```
net/base/network_delegate.h:264:  // when LOAD_DO_NOT_SEND_COOKIES is specified.
net/url_request/url_request.h:910:  // (LOAD_DO_NOT_SEND_COOKIES / LOAD_DO_NOT_SAVE_COOKIES). As described at
```

无任何 `LOAD_FLAG(...)` 定义。再用 tag 源码跨版本核对：

```bash
curl -sS 'https://chromium.googlesource.com/chromium/src/+/refs/tags/<TAG>/net/base/load_flags_list.h?format=TEXT' | base64 -d | grep -c DO_NOT_SEND_COOKIES
```

| Chrome tag | `DO_NOT_SEND_COOKIES` | `DO_NOT_SAVE_COOKIES` |
| --- | --- | --- |
| 86.0.4240.198 | **1**（`LOAD_FLAG(DO_NOT_SEND_COOKIES, 1 << 8)`） | 1 |
| 87.0.4280.141 | **0** | 1 |
| 88–99（逐个 tag 核对） | 0 | 1 |
| 100.0.4896.127 | 0 | 1 |
| 120.0.6099.216 | 0 | 1 |
| 156.0.8078.31 | 0 | 1 |
| 157.0.8103.39 | 0 | 1 |

M86 原文：

```
54:// This load will not send any cookies.
57:LOAD_FLAG(DO_NOT_SEND_COOKIES, 1 << 8)
```

### 结论

- **该 flag 在 M87 被删除**。覆盖区间：**M86 有，M87–M157 都没有**。不是「M156 没有」这么简单。
- 仓库 Core 是 M156，因此 **ABI v12 不能靠它实现 `discard_cookies`**。
- 156 里可用的 per-request 机制是另一个：`URLRequest::set_disallow_credentials()`（`net/url_request/url_request.cc:594`），它同时置 `partial_load_flags_ |= LOAD_DO_NOT_SAVE_COOKIES`。
- **但它有副作用，不能直接当 `discard_cookies` 用**：`DeterminePrivacyMode()`（`net/url_request/url_request_http_job.cc:528-539`）在 `!allow_credentials()` 时返回 `PRIVACY_MODE_ENABLED` 或 `PRIVACY_MODE_ENABLED_WITHOUT_CLIENT_CERTS`，即**连客户端证书行为一起改掉**。

### 对方案的影响

一次性引擎（本轮已落地）**在 M156 上是正确选择，不是权宜之计**：唯一能只影响 cookie、不影响证书与隐私模式的 per-context 开关就是「换一个 store」，而 `CookieMonster` 是 per-context 单例。

若将来要走 ABI：需同时暴露 `send_cookies`，并**显式断言** `send_client_certs` 不受影响，把 `DeterminePrivacyMode()` 的连锁反应写进 ABI 头契约。

---

## T-2 ｜ `58bcc148…` 对应哪个 tag —— **记录正确，是我原先的怀疑不成立**

### 实测

```bash
curl -sS 'https://chromium.googlesource.com/chromium/src/+refs/tags?format=JSON'
```

```
156.0.8078.31 -> 58bcc148f76b8975bbe59f1a6012d4ae409d62a9
157.0.8103.39 -> 9a31f0ac…（只保留前 8 位）
```

157 那一行的 commit 在这里**只写到 8 位**，这是 `tools/audit-pypi-metadata.sh` 的要求：
文档里出现的完整 40 位 commit 必须等于 `CHROMIUM_REVISION`，否则读者会以为本文描述的是
本仓库构建的那棵树。这里引用的是另一个 tag 的 commit，属于「tag 到 commit 的映射」这条
不同的事实，写成缩写既保留证据，也不与门禁的规则冲突。全文缩写与完整值只差后 32 位，
需要完整值时用上面那条 `curl` 重新取。

`CHROMIUM_REVISION` 与 `core/binaries/linux-x86_64/manifest.json` 里的 revision **正是 M156 的正式 tag**。commit 自身的 `Cr-Commit-Position: refs/branch-heads/8078@{#1048}` 印证 `8078` = 156 的 build 号。

仓库门禁复核：

```bash
python3 tools/audit-profile-evidence.py
```

```
863 matched, 0 mismatched, 0 missing locally, 1 unreachable, 0 on a different local revision (864 checked)
```

### 结论

- **二进制 = M156.0.8078.31，不是 M157**。`kCurrentChromiumMajor = 157` 与产物不一致这一点成立，但只影响 `impersonate=None` 时的默认 UA 字符串，不影响任何 profile 的指纹。
- §1.8 的「差一版 / 记录错」二选一，答案是**前者**。

### 附带发现：一条证据路径写错了

唯一的 `UNREACHABLE` 是 `chrome_156 / quiche_h3 / quiche/quic/core/quic_config.cc`。直接探两个 ref 都是 **HTTP 404**，而本地检出里该文件在 `net/third_party/quiche/src/quiche/quic/core/quic_config.cc`。

`AGENTS.md` §9 的九文件清单把 QUICHE 文件写成顶层 `quiche/...`，那是 DEPS 的软链视角，不是 chromium/src 树里的路径。**证据门禁因此永远无法核对这一项**——不是网络问题。

---

## T-3 ｜ HttpCache 单头阶段规则在 M86 是否相同 —— **证实，覆盖 86–157**

### 实测

```bash
curl -sS '.../refs/tags/86.0.4240.198/net/http/http_cache.cc?format=TEXT' | base64 -d | sed -n '1050,1067p'
```

M86：

```cpp
void HttpCache::ProcessAddToEntryQueue(ActiveEntry* entry) {
  // Note the entry may be new or may already have a response body written to
  // it. In both cases, a transaction needs to wait since only one transaction
  // can be in the headers phase at a time.
  if (entry->headers_transaction) {
    return;
  }
```

M120（1547 行）与 M156/157（2070 行）同一断言、同一注释，只是成员从 `entry->headers_transaction` 变成 `headers_transaction_`。

> 注意：`grep "only one transaction can"` 在 M86/M120 上返回 0，因为该注释**跨两行**（`only one transaction` / `can be in the headers phase`）。我第一次据此判「规则不存在」，是错的；换成按 `ProcessAddToEntryQueue` 定位才看到。**跨行注释会让单行 grep 撒谎。**

### 结论

§1.6 的归因可以写「覆盖 86–157」。附带确认：M86 抓取一次返回 0 行是传输截断，重试即得 1484 行——**「0 结果不等于 0 命中」，必须区分**。

---

## T-4 ｜ 并发是否扰动 H2 指纹 —— **证实：稳定**

### 原命令不可用

`tools/align-live-fingerprint.py` 依赖 `target/release/examples/fingerprint_probe`，而该二进制是 11-07 构建的旧 ABI，对着 11-13 的 Core 直接 panic：

```
thread 'main' panicked at crates/minicronet/examples/fingerprint_probe.rs:31:6:
engine: UnsupportedAbi
```

重建需要 `cargo`，本机没有 Rust 工具链（`cargo: not found`）。**记为本机不可用，不记为通过。**

### 替代验证（真跑，不是推断）

用 Python facade 向 `tls.peet.ws/api/all` 发 6 次独立连接（每次新 `Session` = 新引擎 = 新连接）：

| 字段 | 6 次的结果 |
| --- | --- |
| `akamai_fingerprint_hash` | `52d84b11737d980aef856699f885ca86` **×6** |
| SETTINGS | `HEADER_TABLE_SIE=65536, ENABLE_PUSH=0, INITIAL_WINDOW_SIZE=6291456, MAX_HEADER_LIST_SIZE=262144` **×6** |
| 前三个帧类型 | `SETTINGS, WINDOW_UPDATE, HEADERS` **×6** |
| `ja4` | `t13d1516h2_…02713d6af862` ×3，`t13d1517h2_…b1ff8ab2d16f` ×3 |

`ja4` 分成两组需判断是缺陷还是本来如此。查参考抓包：

```
profiles/chrome_120/h2/captures.json  n=6
   ja4: {t13d1517h2_...b1ff8ab2d16f: 2, t13d1516h2_...02713d6af862: 4}
   akamai: {52d84b11...: 6}
```

**参考侧本身就是两个 JA4（4:2）**，Akamai 两侧都是 6/6 单值。所以 JA4 的分散是 Chrome 自己的扩展顺序随机化（`extension_order` 是随机字段），**不是并发扰动**。

### 结论

- H2 指纹（Akamai / SETTINGS / 帧序）在 6 次独立连接下 **6/6 稳定**。
- **§2.3 之后不需要加「H2 指纹并发回归」**：需要的是「多次独立连接下的稳定性回归」，这项已有 `profiles/*/h2/captures.json` 的 6 条证据在守。
- 未验证的部分：真正的**同一条连接上多流并发**的流 ID 顺序。要测它需抓原始 H2 帧，本机没有能解密并解析 H2 帧的工具链。**如实声明为未验证。**

---

## T-5 ｜ ASan 泄漏基线 —— **我给的命令不存在**

### 实测

```bash
grep -n 'asan\|ASAN\|sanitize' tools/build-core-linux-x86*.sh
```

**无输出**。构建脚本从来没有 `--asan` 选项。审计报告 §4.2 里写的 `tools/build-core-linux-x86.sh --asan --out /tmp/core-asan` 是**一条不存在的命令**——照它执行只会因为不认识的参数失败或被忽略，而「跑通了」会被误读为 ASan 版可用。

### 更根本的阻塞

```
out/MiniCronet-linux-x86_64/args.gn:
  is_official_build = true
  symbol_level = 0
  is_debug = false
```

`is_official_build = true` 与 ASan 互斥，`symbol_level = 0` 会让 ASan 报告没有符号。要跑 ASan 必须**另开 out 目录、另写 args.gn、重编 net/quiche/boringssl**，数小时级，且产出的不是可发布产物。

### 结论

- ASan 基线**本机不可得**，且不是「装一下就行」。
- §4.2 里「ASan 判据能否进 CI」：**当前不能**。构建脚本没有这条路径，`args.gn` 与 ASan 冲突，且没有门禁覆盖 sanitizer 构建。
- 替代（已在用）：`tools/leak-hunt.py` 的 fd/RSS 斜率 + 关 GC 的 `churn-nogc`。它能发现「随请求数线性增长」，发现不了「一次性的堆越界」——**这个空白要如实留着，不能拿斜率干净去冒充内存安全。**

---

## T-6 ｜ 512 并发下的 loop 停摆 —— **文档表格已不可复现，原因是工具改了**

### 实测（用文档指定的命令）

```bash
python3 tools/async-diag.py flood --ladder 64,256,1024 --requests 1024
```

```
limit    qps       p99      lag_p99   rss_peak  tasks_peak
64       952.6     926.116  6.82      48388     1026
256      958.0     802.209  13.22     52020     1026
1024     936.4     854.579  7.41      62200     1026
```

对照 §2.2：QPS 622/685/857、lag_p99 11.9/**20.5**/**1178**。**lag_p99 = 1178 ms 那个「H1 的量化」复现不出来。**

### 原因：不是行为变了，是工具测的形状变了

```python
# tools/async-diag.py:150
urls = ["%s/small?size=1024&n=%d" % (base, i) for i in range(args.requests)]
```

`flood` 现在给**每个请求一个不同 URL**，所以 HttpCache 不串行化；而文档那张表是在**单个 URL** 上测的。

用单 URL 复现同一阶梯（`tools/loop-lag-check.py`，同一 fixture）：

```
limit=64    qps= 431.3  lag_p99= 1.8ms   lag_max= 15.2ms
limit=256   qps= 167.2  lag_p99= 1.6ms   lag_max= 56.4ms
limit=1024  qps=  70.3  lag_p99= 1.6ms   lag_max= 236.3ms
```

**lag_p99 恒为 1.6 ms，峰值 236 ms——不是 1178 ms。** 单 URL 时循环很健康，慢的是 Core 的 HttpCache 排队（qps 431 → 70）。

### 结论

- §2.2 的 **lag_p99 = 1178 ms 不能再引用**：它测的形状与表头声称的不一致，且无法用当前工具复现。
- 它想表达的「循环被同步前缀占住」需用**不同 URL** 的形状来测——那种形状下确实会出现 lag 峰值（本会话在 512 不同 URL 上测到 295 ms）。
- **§1.5 的订正必须连带改 §2.2 那张表**：把「单 URL」换成「不同 URL」并重测。这也正是我给 §2.2 加注记时想指出的，但当时没有把表本身改掉。

复现探针已落成 `tools/loop-lag-check.py`（`--shape one-url|distinct|both`，`--cache-mode` 可把 HttpCache 排队单独关掉），本机实测 one-url `lag_p99 1.42 ms / max 101 ms`、distinct `lag_p99 1.28 ms / max 90 ms`，量级与本节一致。

---

## 汇总

| 编号 | 原判断 | 实测结论 | 是否改变方案 |
| --- | --- | --- | --- |
| T-1 | 「待验证」 | flag **M87 已删**；替代机制有证书副作用 | **是**：一次性引擎是正确解 |
| T-2 | 「待验证」 | 二进制 = **M156.0.8078.31**，常量 157 是错的 | 否（只影响默认 UA） |
| T-3 | 「待验证」 | 规则**覆盖 86–157** | 否（证实） |
| T-4 | 「待验证」 | H2 指纹 6/6 稳定；JA4 分散是 Chrome 自己随机化 | **是**：不需新增并发回归 |
| T-5 | 「给了一条命令」 | **命令不存在**；ASan 与 `is_official_build` 冲突 | **是**：ASan 不能进 CI |
| T-6 | 「待验证」 | 文档表格**测的形状与表头不符**，1178 ms 不可复现 | **是**：§2.2 要重测 |
| T-7 | 无（本次新增） | type-65 查询已用 strace 证明发出；`ech_status=success` 与 `sni=encrypted` 已用 Cloudflare DoH 端到端验证。未自动化的只剩「默认 resolver 这一格」 | **是**：CI 需要能绑 53 的容器 |

三条改写结论（T-1、T-4、T-5），一条要求改文档（T-6），一条指出我自己的命令是错的（T-5）。这五条里有四条只看「有没有配好」是发现不了的——必须真的跑，或者真的读 tag 源码。

---

## T-7 ｜ 默认路径是否真的发出了 DNS HTTPS 查询 —— **查询已证发出，`success` 已端到端验证；仅「默认路径」这一格未自动化**

### 为什么它在测试套件里坐不了

默认配置走系统解析器，要证明它发了 type 65，只能控制它说话的那台 nameserver。
本机实测：

```bash
cat /proc/sys/net/ipv4/ip_unprivileged_port_start
# 1024
python3 -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('127.0.0.1',53))"
# PermissionError: [Errno 13] Permission denied
unshare -rmn true
# unshare: write failed /proc/self/uid_map: Operation not permitted
```

所以 `bindings/python/tests/test_session_defects.py` 里的
`DefaultResolverQueriesHttpsRecordTests` 只断言**能观测到的那一半**：传 `dns_nameservers` 之后
类型 65 与类型 1 都会出现。这两条在改动前后的二进制上都通过，因此它们**不能**用来证明本次改动；
这一点已写在测试的 docstring 里，而不是靠读者猜。

### 已经拿到的证据（本次实测，可重放）

用 `strace -f -xx -e trace=sendto` 对比改动前后的默认路径，直接看 DNS 问题类型：

```bash
# 旧二进制（git show HEAD:core/binaries/linux-x86_64/libminicronet.so）
LD_LIBRARY_PATH=/tmp/oldcore PYTHONPATH=bindings/python \
  strace -f -xx -e trace=sendto -o /tmp/old.txt /usr/bin/python3.12 /tmp/probe.py
# 新二进制
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python \
  strace -f -xx -e trace=sendto -o /tmp/new.txt /usr/bin/python3.12 /tmp/probe.py
```

| 二进制 | 默认路径发出的 DNS 问题 |
| --- | --- |
| 改动前 | `example.com type=1` |
| 改动后 | `example.com type=65` 与 `example.com type=1` |

端到端那一段也用本地应答器验过，不需要外网 DNS：

```bash
# 应答器对 type 65 回一条带 ech= 的 HTTPS 记录（SVCB 参数 key 5）
dig +short HTTPS crypto.cloudflare.com @127.0.0.1 -p 15353
# 1 . alpn="h2" ech=AEX+DQBBewAgACAo4AWByYxcZhs8/EeGpTXNqde2kR1Vx+9sx2fVKDzXIQAEAAEAAQASY2xvdWRmbGFyZS1lY2guY29tAAA=

LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python python3 -c \
  "from chrome_client import Session;
   s = Session(impersonate='chrome_158', dns_nameservers='127.0.0.1:15353');
   r = s.get('https://crypto.cloudflare.com/cdn-cgi/trace');
   print(r.ech_status); print([l for l in r.text.splitlines() if l.startswith('sni=')])"
# success
# ["sni=encrypted"]
```

### 补充：本机明文 DNS 一律拿不到 type 65（2026-11 实测）

上面写「systemd-resolved stub 对 HTTPS 查询回空应答」，这句话当时只查了 stub，把范围
说窄了。逐个问过本机能触达的解析器之后，结论是所有**明文** DNS 都不回答 type 65：

| 解析器 | `A crypto.cloudflare.com` | `HTTPS crypto.cloudflare.com` |
| --- | --- | --- |
| `127.0.0.53` | `198.18.1.38` | NOERROR, ANSWER: 0 |
| `1.1.1.1` | `198.18.1.39` | NOERROR, ANSWER: 0 |
| `8.8.8.8` | — | NOERROR, ANSWER: 0 |
| `223.5.5.5` | — | NOERROR, ANSWER: 0 |
| `192.168.179.2` | — | NOERROR, ANSWER: 0 |
| `cloudflare-dns.com`（DoH） | 真实地址 | 带 `ech=` |

`198.18.1.x` 属于 RFC 2544 保留测试段，即这条链路的 DNS 被中间设备改写。这解释了为
什么默认路径在这台机器上永远 `not_attempted`：不是引擎没发查询（strace 已证明发了
`type=65`），而是应答在到达客户端之前就空了。

### 端到端：换成能答 type 65 的 DoH 就加密

这不依赖本地应答器，也不需要 root，是本次可以直接重放的端到端证据：

```bash
LD_LIBRARY_PATH=core/binaries/linux-x86_64 PYTHONPATH=bindings/python python3 -c "
import chrome_client as c
s = c.Session(doh_template='https://cloudflare-dns.com/dns-query',
              dns_secure_mode=2, timeout=20)
r = s.get('https://crypto.cloudflare.com/cdn-cgi/trace', timeout=20)
print(r.ech_status); print(r.text)"
# success
# ... sni=encrypted
```

三次对照，同一产物、同一 URL：

| 解析器 | `ech_status` | 服务端看到的 SNI |
| --- | --- | --- |
| 默认（`127.0.0.53`） | `not_attempted` | `sni=plaintext` |
| `cloudflare-dns.com` DoH | `success` | `sni=encrypted` |
| `dns.google` DoH | `not_attempted` | `sni=plaintext` |

Cloudflare DoH 连跑 5 次全部 `success` / `sni=encrypted`。`dns.google` 那行同样是
`not_attempted`，原因不是 DoH 不通（它的 DoH 端点返回 200），而是它对 `crypto.cloudflare.com`
给出的 HTTPS 记录里没有可用的 `ech=`；没有 ECHConfigList 时 ECH 只能发 GREASE。
所以 `not_attempted` 在此时的含义是「没拿到配置」，不是「功能被关掉」——这一点必须
写清楚，否则读者会把它当成引擎的缺陷。

这条证据补上了 strace 对比缺的另一半：strace 只能证明查询发出去了，这里能证明「查询
成功 → 拿到 ech= → SNI 真的加密 → 服务端确认 `sni=encrypted`」整条因果链成立。
### 还差什么才能自动化

需要一样东西：能绑 53 端口、并被 `/etc/resolv.conf` 指向的 responder。三种可行形态：

1. CI 上用 `sysctl net.ipv4.ip_unprivileged_port_start=53` 的容器（或直接以 root 跑该测试）；
2. 给容器加 `CAP_NET_BIND_SERVICE`；
3. `unshare -rmn` + `--map-root-user`（本机被拒，多数发行版默认关闭）。

拿到任一条之后，在 `DefaultResolverQueriesHttpsRecordTests` 里补一条默认路径用例：
不传任何 `dns_*`，断言 53 端口上的应答器收到 type 65。
本机现在能做的等价证据就是上面那段 strace 对比，它已经证明了
`engine.cc` 的改动生效（旧产物 `type=1`，新产物 `type=65 + type=1`）。

---

## T-8: a real QUIC proxy tunnel was never established

**Status:** open. The `quic://` proxy scheme parses, reaches Chromium as
NaN`SCHEME_QUIC``, and produces a genuine QUIC v1 Initial packet at the proxy
port (six datagrams, long-header bit set, version ``00000001``, no plaintext SNI).
What is unproven is everything after the ClientHello.

**Why it is open.** No QUIC proxy server was reachable from this host. Chromium
needs a peer that speaks CONNECT over QUIC (``quic_proxy_client_socket.cc`` drives
it); a bare UDP socket can only witness that packets were sent, which is what the
capture above records. A handshake against a socket that never replies ends in
NaN`ERR_QUIC_HANDSHAKE_FAILED (-358)`` or ``ERR_TIMED_OUT (-7)``, both of which are
consistent with a real attempt and with a wrong port, so neither proves the tunnel.

**How to close it.** Point ``Session(proxy="quic://HOST:PORT")`` at a QUIC
proxy (Chromium's own ``net/quic/quic_proxy_client_socket_unittest.cc`` fixtures, or
any MASQUE / CONNECT-over-QUIC implementation), request an ``https://`` origin, and
assert both that the response body arrives and that the proxy logs the CONNECT.
Until then ``MN_CAPABILITY_QUIC_PROXY`` reports that the build can *dial* a QUIC proxy,
not that a tunnel has been demonstrated.
