# 方案：让 IDE 完整显示所有请求参数

> 目标：`requests.get/post`、`Session(...)`、`session.get/post` 等方法在 IDE 里悬浮/补全时
> **列出全部参数**，`impersonate` 悬浮即见全部取值。
> 约束：只改 `.pyi`（及生成器/审计脚本），**运行时零改动**，API 名称不变。
> 状态：**方案 A 已实施（2026-09-17），未提交。** 实施记录见文末。

---

## 一、结论：目前哪些参数看不到

当前存根（0.2.4）把每个入口分成了三种待遇，只有第 1 类是"全显式"：

### 1. 已经全部可见（显式参数，任何 IDE 都能看到）

| 入口 | 参数数 | 说明 |
| --- | --- | --- |
| `Session(...)` / `AsyncSession(...)` | 38 | 构造函数逐个显式，含 `impersonate` |
| `session.request(method, url, ...)` | 52 | 与模块级 `request()` 一致 |

这也是 `Session(impersonate=)` 悬浮能看到完整 `Literal` 的原因——和 tls_client 的
`client_identifier` 是同一类（**构造函数的显式参数**）。

### 2. 只能看到"前半截"（其余藏在 `**kwargs: Unpack[RequestOptions]`）

| 入口 | 你能看到 | 看不到 |
| --- | --- | --- |
| `requests.get(url, params=None, **kwargs)` | `url`、`params` | 其余 **38 个** |
| `requests.post(url, data=None, json=None, **kwargs)` | `url`、`data`、`json` | 其余 **38 个** |
| `requests.put/patch(url, data=None, **kwargs)` | `url`、`data` | 其余 38 个 |
| `requests.delete/head/options/trace/query(url, **kwargs)` | `url` | 其余 38 个 |
| `session.get(...)`（同上 9 个 verb） | 同 requests | 同 requests |
| `AsyncSession` 的 9 个 verb | 同上 | 同上 |
| `chrome_client.session(**kwargs)` / `async_session(**kwargs)` | 无 | **全部 38 个构造参数** |
| `session.stream("GET", url, **kwargs)` | `method`、`url` | 其余 38 个 |

藏起来的 38 个键（`RequestOptions`）：

```
headers, cookies, files, auth, timeout, allow_redirects, proxies, hooks, stream,
verify, cert, content, multipart, impersonate, proxy, http_version, max_redirects,
max_response_bytes, referer, accept_encoding, default_encoding, discard_cookies,
retry, cache_mode, priority, ja3, akamai, perk, extra_fp, content_callback,
raise_for_status, quote, curl_options, interface, doh_url, max_recv_speed,
thread, debug
```

> **注意 `session()` / `async_session()`**：`Session(...)` 能看到 `impersonate`，但同样一个参数
> 在 `chrome_client.session(impersonate=...)` 里就看不到——两处不一致，值得一并修掉。

### 3. 完全不可见，且写了会**报类型错误**（存根漏列，运行期其实支持）

| 参数 | 出现在哪些 verb | 运行期行为 |
| --- | --- | --- |
| `json` | get / put / patch / delete / head / options / trace / query | 转发给 `request(json=...)`，**正常生效** |
| `data` | get / delete / head / options / trace / query | 转发给 `request(data=...)`，**正常生效** |

这一类比第 2 类更糟：`session.get(url, json={...})` 在 IDE 里**直接标红**，但运行期是完全正确的
（`RequestOptions` 里没有 `json`/`data`，因为它们在 `request()` 里是显式参数，PEP 692 不允许
同名键出现在 `Unpack[...]` 中）。

---

## 二、根因

1. **`**kwargs: Unpack[TypedDict]`（PEP 692）的可见性完全取决于 IDE。**
   pyright/Pylance 支持并会展开；**PyCharm / JetBrains 支持很弱或没有**，只会显示一个
   `**kwargs`。你贴出的悬浮文案 `Parameter client_identifier of
   tls_client.requests_compat.Session.__init__` 是 **JetBrains 的格式**，所以这套存根在你的 IDE
   里，verb 方法的参数只会显示显式的 `url`/`params`——正是你观察到的现象。

2. **tls_client 的 verb 方法其实也是 `**kwargs: Any`**（`def get(self, url, **kwargs: Any)`），
   它的参数同样看不到；它"好看"的只有构造函数。**所以纯粹照抄 tls_client 并不能解决 verb 方法
   的问题**——要解决就得比它更进一步：把 verb 也写成显式参数。

3. **`data`/`json` 未列入 `RequestOptions`**，导致第 3 类漏列。

4. **`Impersonate` 目前是 `Union[ChromeProfileName, ChromeProfileAlias, ChromeFamilyAlias]`**，
   不是 tls_client 那种单个扁平 `Literal`；悬浮时显示为三段 Union，不如一个完整列表清晰。

5. **存根硬依赖 `typing_extensions`，但它没有声明进 wheel 依赖。**
   12 个 `.pyi` 都写着 `from typing_extensions import Final, Literal, Never, TypeAlias,
   TypedDict, Unpack, overload`，而 `bindings/python/pyproject.toml` 的 `[project]` 里
   **没有** `dependencies`（`pip install chrome-client` 不会装 `typing_extensions`）。
   于是解释器环境里若没有这个包（它只是"类型检查期依赖"，未随 wheel 分发），
   **PyCharm 解析这些 `.pyi` 时会失败**：`Literal`、`TypeAlias`、`Unpack` 全部退化为
   Unknown，别名 `Impersonate` 解析不出来，verb 方法的参数集体消失——和"只看到
   `url`/`params`"的症状也吻合。

   > **已核实（2026-09-17）：用户环境里 `typing_extensions` 是装着的**
   > （`python -c "import typing_extensions"` 成功；它本身不提供 `__version__`，所以那条
   > `AttributeError` 属正常现象）。**因此这一条不是当前症状的根因，降级为"锦上添花的
   > 健壮性改进"**——仍建议顺手做掉，让没装 `typing_extensions` 的环境也能用，且不改变
   > wheel 的 `Requires-Dist`。

   修复方式（可选，优先级低于方案 A）：改成 typeshed 标准的**版本条件导入**，
   让 Python ≥ 3.8 的环境彻底不依赖 `typing_extensions`：

   ```python
   import sys
   if sys.version_info >= (3, 8):
       from typing import Final, Literal, Protocol, TypedDict, overload
   else:
       from typing_extensions import Final, Literal, Protocol, TypedDict, overload
   if sys.version_info >= (3, 10):
       from typing import TypeAlias
   else:
       from typing_extensions import TypeAlias
   ```

   - `Literal` / `TypedDict` / `Protocol` / `Final` / `overload`：**3.8+ 就在 `typing` 里**
   - `TypeAlias`：3.10+ 在 `typing` 里
   - `Never`：3.11+；本项目只在 `RawStream.tell()` 用过，可换成 `NoReturn`（3.6+ 就有）
   - `Unpack`：**3.11+ 才有**，而方案 A 正好弃用它 → 依赖消失

---

## 三、修改方案

### 方案 A（推荐，**已确认采用**）：所有 verb 方法改成显式参数

> 已确认：使用 **PyCharm**，并要求"尽可能兼容更多 IDE"。**显式具名参数是所有 IDE 的最低
> 公分母**，所以这是唯一能满足要求的选择——`Unpack[TypedDict]` 只在 pyright/Pylance/mypy
> 下有效，PyCharm 及多数编辑器不支持。

#### IDE 兼容性矩阵

| 特性 | PyCharm | VS Code / Pylance | mypy / pyright | 其他编辑器 |
| --- | --- | --- | --- | --- |
| 显式具名参数 | ✅ | ✅ | ✅ | ✅ |
| 类型别名展开（悬浮列出 `Literal`） | ✅ | ✅ | ✅ | ✅ |
| `@overload` | ✅ | ✅ | ✅ | 多数 ✅ |
| `if sys.version_info` 条件导入 | ✅ | ✅ | ✅ | ✅ |
| `**kwargs: Unpack[TypedDict]` | ❌ / 很弱 | ✅ | ✅ | ❌ |
| 缺 `typing_extensions` 时别名可解析 | ❌ | ❌ | ❌ | ❌ |

改完之后，全部特性都落在前三行 —— 任何 IDE 都能完整显示。

#### 具体改法

把 `**kwargs: Unpack[RequestOptions]` 展开成逐个具名参数，形状与 `request()` 对齐：

```python
# 改前
def get(self, url: str, params: Optional[ParamsLike] = None,
        **kwargs: Unpack[RequestOptions]) -> Response: ...

# 改后
def get(
    self,
    url: str,
    params: Optional[ParamsLike] = None,
    data: Optional[Body] = None,
    json: Any = None,
    headers: Optional[HeadersLike] = None,
    cookies: Optional[CookiesLike] = None,
    auth: AuthLike = None,
    timeout: Timeout = None,
    allow_redirects: bool = True,
    proxies: Optional[Proxies] = None,
    verify: Optional[Verify] = None,
    stream: Optional[bool] = None,
    impersonate: Optional[Impersonate] = None,
    http_version: Optional[HttpVersion] = None,
    max_redirects: Optional[int] = None,
    # ... 其余全部（含被拒绝的 cert/ja3/... 标注为 None）
) -> Response:
    """...（现有中文 docstring 保留，并补 Args: 逐项说明）..."""
    ...
```

**覆盖清单（共 29 个方法 + 3 处）**

| 位置 | 方法 | 数量 |
| --- | --- | --- |
| 模块级 `api.pyi` | get, post, put, patch, delete, head, options, trace, query | 9 |
| `sessions.pyi` `Session` | 同上 9 个 | 9 |
| `sessions.pyi` `AsyncSession` | 同上 9 个（`async def`） | 9 |
| `api.pyi` | `session()`, `async_session()` → 展开 `SessionOptions` | 2 |
| `sessions.pyi` | `_StreamFlag.__call__`, `_AsyncStreamFlag.__call__` | 2 |

**同时**：

- 补齐第 3 类的 `data` / `json`（运行期已支持，写上去就变得可见且不再误报）。
- `Impersonate` 改为**单个扁平 `Literal`**（112 个值，用注释分三组：规范名 / curl-cffi 拼法 /
  家族名），与 tls_client 的 `ClientIdentifiers` 结构一致；三个分量别名保留供内部使用。
- **不加** `**kwargs: Any` 兜底——否则拼错的参数会被静默接受，正是要避免的。
- `Unpack[RequestOptions]` / `Unpack[SessionOptions]` 随之退役（PEP 692 不允许显式参数与
  Unpack 键重名）。TypedDict 定义可保留作文档用途，或直接删除。
- **12 个 `.pyi` 的 `typing_extensions` 导入全部改为版本条件导入**（见根因 5），
  `Never` 换成 `NoReturn`。这一步独立于方案 A，**本身就能修掉一大半"看不到"的问题**，
  建议优先单独做、单独验证一次。

**维护方式（关键）**：29 个签名 × ~45 参数 ≈ 1300 行，**不要手写**。
在 `tools/generate-python-stubs.py` 里建一张**单一参数规格表**（参数名、类型、默认值、docstring
文案、哪些动词适用），由它生成全部 verb 签名。改一处、全量同步。

**审计扩展**：`tools/audit-python-stubs.py` 增加断言——每个 verb 存根的参数名集合必须等于
运行期 `Session.request()` 接受的集合。防止以后再漂移。

### 方案 B（最小改动）：只把高频参数显式化

显式：`headers, cookies, data, json, timeout, allow_redirects, verify, proxies, auth, stream,
impersonate, http_version, max_redirects`；其余留给 `**kwargs`。
**代价**：因 PEP 692 约束，要从 `RequestOptions` 删掉这些键（或另建"剩余键"TypedDict），
维护更绕；且 PyCharm 用户仍看不到剩余 20 多个参数。**不推荐。**

### 方案 C（不推荐）：维持现状

只修 `Impersonate` 的形状 + 补 `data`/`json`。构造函数的可见性不受影响，但 verb 方法在
PyCharm 里依旧只有 `url`/`params`。

---

## 四、影响面

| 项目 | 影响 |
| --- | --- |
| 运行时 `.py` | **零改动** |
| API 名称 / 默认值 / 行为 | **不变** |
| 存根体积 | verb 签名约 +1300 行（生成器产物，非手写） |
| 现有审计 / CI | 全绿，另加一条"参数集合一致性"断言 |
| 发布 | 需升版本才能到用户手上（当时按 0.2.5 计划）；0.2.4 已发布，PyPI 不可重传 |
| `Unpack[RequestOptions]` | 退役；`_types.pyi` 的 TypedDict 可留作文档 |

---

## 五、确认情况与实施顺序

### 已确认

- IDE 是 **PyCharm**，并要求尽量兼容更多 IDE → **采用方案 A（全显式参数）**。

### 实施顺序（根因 5 已排除后）

根因 5 已核实**不是**当前症状的原因（用户环境装了 `typing_extensions`）。于是剩下唯一
根因：**PyCharm 不支持 PEP 692 `Unpack[TypedDict]` 的参数展开**。

⇒ **方案 A 不再是"可选的大改"，而是唯一的解法**。原计划的"两步走"合并为一次实施：

1. **29 个 verb 方法 + `session()` / `async_session()` + 两个 `stream.__call__` 展开为显式具名参数**
   —— 这是解决问题的主体。
2. `Impersonate` 改成单个扁平 `Literal`（悬浮更清晰）。
3. 补齐 `data` / `json`（顺带解决"写了报错"的第 3 类）。
4. 条件导入（`typing_extensions`）作为顺带的健壮性改进。
5. 生成器从单一参数表产出全部 verb 签名；审计脚本加"参数集合一致性"断言。
6. 发版（该轮随 0.2.5 发布；后续 0.2.6 补上了代理认证与 Referer 回归测试）。

### 需要你确认

1. **你现在装的是哪个版本？** 请在 PyCharm 终端跑：

   ```bash
   pip show chrome-client
   ```

   若是 **0.2.3**，则你当前看到的 `url`/`params` 来自运行时 `.py`（0.2.3 的 wheel 里
   **一个 `.pyi` 都没有**），需要先升到 0.2.4 才有存根可用；若是 **0.2.4**，那症状就是
   PyCharm 的 `Unpack` 限制，本方案直接对症。

2. **确认 PyCharm 右下角选中的解释器就是这个 `(env)` 环境**（否则改了也看不到）。

3. **是否现在开始实施？** 改动只落在 `.pyi` 与生成器/审计脚本，**运行时零改动**、
   可随时 `git reset` 撤销。

### 沿用上次的结论（如无异议就这么办）

- 被 ABI v8 拒绝的 `cert/ja3/akamai/perk/interface/doh_url/curl_options/thread/debug`
  在类型上标注为 `None`（写错即标红），`max_recv_speed` 用 `Literal[0, None]`。
- **不加** `**kwargs: Any` 兜底。

---

## 实施记录（2026-09-17）

方案 A 落地，运行时零改动：

- **29 个 verb + `session()`/`async_session()` + 两个 `stream.__call__` 全部展开为显式具名参数**
  （模块级 52 个参数、Session 级 42 个、构造 37 个、stream 40 个+`method`/`url`）；
  docstring 的 `Args:` 段同步生成全部参数的说明，摘要/Returns/Example 逐字保留。
- `data`/`json` 补到每个 verb（顺带修掉"运行期支持但类型上报错"的缺陷）；
  `head` 的 `allow_redirects` 默认 `False`；`stream(...)` 不列 `stream`。
- `Unpack` 全部退役：`api.pyi` 不再依赖 `typing_extensions`；`_types.pyi` 的
  `RequestOptions`/`SessionOptions` 保留为参数集合的权威描述。
- 新工具 `tools/expand-verb-signatures.py`（幂等，`--check` 断言 verb 参数表与
  `request()`/`__init__` 一致），已并入 `tools/audit-python-stubs.py`（CI 会跑）。
- review 后续：`default_encoding` 模块级过窄已修（`Optional[Union[str, Callable]]`）；
  `Session.put/patch` 的 `json` 位置参数已改为关键字；`send(request, ...)` 的 19 个
  options 已从 `**options: Any` 展开为显式参数（`request()` 参数的子集 +
  `native_redirects`/`python_redirects`）。
- 验证：审计 0 error；mypy 存根 0 错误；pyright `.pyi` 诊断与改动前完全相同（8 条既有项）；
  参数覆盖测试——正面 30+ 个关键字全部通过，负面 `chrome_200`/`timout` 被 mypy 与
  pyright 同时标红；回归 103 用例 OK（2 skip 既有）。
- 破坏性验证：删掉 `Session.get` 的 `impersonate`，审计立刻报
  `stale: Session.get (sessions.pyi)`。

**未做（待定）**：被 ABI v8 拒绝的 `cert`/`ja3`/`akamai`/`perk`/`interface`/`doh_url`/
`curl_options`/`thread`/`debug` 目前类型仍是 `Optional[str]`/`Any`（写真值不报错，运行期
抛 `UnsupportedFeature`）。收窄成 `None` 的改动此前以 `11bf899` 提交过、后按用户要求撤销；
用户需求里"未实现的要显式标注"指向它，**需用户再次确认后**再做。
