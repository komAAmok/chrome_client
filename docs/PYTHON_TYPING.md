# Python 类型提示

本文说明 `chrome_client` 的类型提示契约：覆盖范围、为什么放在 `.pyi` 而不是内联标注、
用到的类型手法，以及存根和实现之间如何保持不漂移。

## 目标与约束

- **最大化编辑器效果**：参数提示、方法补全、悬停文档。为此每个公开入口的每个参数都有
  明确类型，`impersonate` 用 `Literal` 列出全部可用值，`**kwargs` 用 `TypedDict` 描述。
- **不改变现有 API 行为**：`bindings/python/chrome_client/**/*.py` 一个字节都没动，
  运行时导入路径、异常类型、返回值与之前完全一致。回归成绩：`test_stability` +
  `test_compat` 共 103 个用例（2 个因缺少本地 WS/WSS 端点跳过）全部通过。

## 为什么用 `.pyi`，而不是把标注写进 `.py`

同一套 `.py` 会被打进两个 wheel：`bindings/python`（cp37-abi3）与 `bindings/python36`
（cp36-abi3，`python-source = "../python"`）。也就是说这些模块必须能在 **Python 3.6**
上运行。由此产生两个硬约束：

- Python 3.6 没有 `from __future__ import annotations`（3.7 才有），函数标注会在导入时
  求值；`Literal`、`Protocol`、`TypedDict`、`Unpack` 都不在 3.6 的 `typing` 里。
- 要让内联标注成立，就得把 `typing_extensions` 变成**运行时依赖**，并给每个模块加上
  `if TYPE_CHECKING` 分支，这既改了发布物的依赖面，也让 3.6 路径多出一层风险。

存根文件不参与导入，所以：

- 可以用现代语法（`Literal` / `TypedDict` / `Unpack` / `Never` / `TypeAlias`），
  `typing_extensions` 只是**类型检查期**依赖，wheel 的 `Requires-Dist` 不变；
- 递归注解、前向引用、`if TYPE_CHECKING` 里的循环导入都无需运行时关照；
- 注释和 docstring 不会增加解释器启动成本（`py.typed` 只告诉类型检查器"这里带类型"）。

代价是：改了运行时签名必须同步存根，否则两者会静默分叉——这正是
`tools/audit-python-stubs.py` 存在的原因（见文末）。

## 覆盖范围

存根按运行时模块一一对应，位于 `bindings/python/chrome_client/`：

| 文件 | 覆盖的公开面 |
| --- | --- |
| `_python_impl/_types.pyi` | 全部类型别名、`Protocol`、`RequestOptions` / `SessionOptions` 两个 `TypedDict` |
| `_python_impl/impersonate.pyi` | `impersonate` / `http_version` 的 `Literal`、`CurlHttpVersion`、`ExtraFingerprints` |
| `_python_impl/sessions.pyi` | `BaseSession`、`Session`、`AsyncSession`、`Client`、`AsyncClient`、`RetryStrategy` |
| `_python_impl/models.pyi` | `Request`、`PreparedRequest`、`Response`、`AsyncResponse`、`RawStream` |
| `_python_impl/websockets.pyi` | `WebSocket`、`AsyncWebSocket`、`WsCloseCode`、`CurlWsFrame` |
| `_python_impl/api.pyi` | 模块级 `get`/`post`/… 与 `session()` / `async_session()` |
| `_python_impl/cookies.pyi` | `RequestsCookieJar`、`CookieJar`/`Cookies`、cookie 辅助函数 |
| `_python_impl/structures.pyi` | `CaseInsensitiveDict`、`Headers`、`LookupDict` |
| `_python_impl/{exceptions,auth,adapters,engine,multipart,utils,status_codes}.pyi` | 其余公开面 |
| `chrome_client/*.pyi`、`chrome_client/requests/*.pyi` | 门面：把上述名字按 PEP 484 的 `X as X` 形式再导出 |

规模：47 个存根文件、约 8,400 行；`_python_impl` 里 358 个公开可调用对象，344 个带
docstring（Args/Returns/Yields/Raises/Example）。剩下 14 个是**故意**不写的：属性 setter
（文档在 getter 上）、`@overload` 的第二个及以后变体（与 typeshed 惯例一致，文档在第一个
变体上）、以及 `_StreamContext` / `_AsyncStreamContext` 这两个私有类的 dunder。

门面文件不是手写的：`_python_impl/*.pyi` 是唯一真源，顶层与 `requests/` 下的同名模块由
`tools/generate-python-stubs.py` 按公共名列表生成，避免"新增一个公开名字，门面忘了导出"。

## 关键类型手法

### `Literal`：`impersonate` 与 `http_version`

`impersonate=` 的类型是三个 `Literal` 的并集：

- `ChromeProfileName`：`chrome_99` … `chrome_153`，共 55 个规范名；
- `ChromeProfileAlias`：curl_cffi 拼法 `chrome99` … `chrome153`，共 55 个；
- `ChromeFamilyAlias`：`chrome`、`chromium`，解析到最新 pinned 版本。

悬停即能看到全部 112 个值，且 `impersonate="chrome_200"`、`impersonate="edge99"` 这类
运行期才会抛 `ImpersonateError` 的写法在写代码时就被标红。`http_version` 同理列出了
`v1`/`v2`/`v3`、数字与 `http/2` 等全部拼法。

这四个 `Literal` 都由运行时同一组常量（`OLDEST_CHROME`/`LATEST_CHROME`/`HTTP_VERSIONS`）
推导：`tools/generate-python-stubs.py` 负责重写，`--check` 用于 CI 断言。新增 pinned 版本时
不要手改存根——手写 112 个字符串迟早会漏一个，悬停里就会少一个版本。`Literal` 里**故意不放**
`str`：一旦并入 `str` 类型就退化成"任意字符串"，补全与拼写检查都会失效；docstring 里说明了
非 Chromium 家族与范围外版本会在运行期显式报错，不会静默降级。

### `Unpack[TypedDict]`：可补全的 `**kwargs`

`Session.get("…", imp<tab>` 能补出 `impersonate=`，靠的是 PEP 692：

- `_types.RequestOptions`（`total=False`）列出 `request()` 接受的全部关键字，
  verb 方法写 `**kwargs: Unpack[RequestOptions]`；
- `_types.SessionOptions` 覆盖构造函数，`session(**kwargs)` / `async_session(**kwargs)` 使用它；
- `post`/`put`/`patch` 把 `data`、`json` 显式声明（PEP 692 不允许参数名与 TypedDict
  键重叠，所以这两个键不在 `RequestOptions` 里）；
- 模块级 `api.request()` 的签名与运行期完全一致地逐个列出 40 余个参数——它同时接受请求
  选项和构造函数选项，枚举比 `Unpack` 在这个位置上更好用。

副作用是 TypedDict 是封闭的：拼错的选项会报 `Unexpected keyword argument`。这正是想要的效果。

### `@overload`：返回值随参数而变

- `Response.iter_content(..., decode_unicode=True)` → `Iterator[str]`，否则 `Iterator[bytes]`；
  `AsyncResponse.aiter_content`/`aiter_bytes`/`aiter_lines`、`Response.iter_lines` 同理。
- `Session.resolve_redirects(..., yield_requests=True)` → `Iterator[PreparedRequest]`，
  否则 `Iterator[Response]`。
- `RawStream.tell()` 标注为 `Never`：它必定抛 `IOError`，标注 `int` 会诱导写出永远失败的代码。

### 有意的 `# type: ignore`

少数位置存根刻意偏离超类型，注释里都写了原因：

| 位置 | 原因 |
| --- | --- |
| `RequestsCookieJar` 类头 | `cookielib.CookieJar` 与 `MutableMapping` 对 `__iter__` 的定义冲突（`for c in jar` 产出 `Cookie`），requests 本身也是这个形状 |
| `RequestsCookieJar.get/keys/values/items/update` | requests 额外支持 `domain`/`path` 过滤并返回 `list`，与 `Mapping` 的签名不同 |
| `Headers.update` | 允许 `Headers`、映射、键值对序列三种入参 |
| `AsyncSession.upkeep` | curl_cffi 里是 async 协程，基类实现是同步 |
| `AsyncSession.stream` | 运行期是同一个 `int` 子类；存根把它收窄成异步流上下文，`async with session.stream(...)` 才能推断出 `AsyncResponse` |

## 校验方式

```bash
# 1) 存根与运行时的一致性（纯 stdlib，不需要 Core）
python3 tools/audit-python-stubs.py

# 2) 存根本身的类型检查（需要 mypy；typing_extensions 由 typeshed 提供）
cd bindings/python && mypy chrome_client --python-version 3.10 --ignore-missing-imports

# 3) 消费者视角：逐条断言 IDE 会显示什么（每个 reveal_type 都有期望注释）
MYPYPATH=$PWD/bindings/python mypy tools/typecheck-consumer.py \
  --python-version 3.10 --ignore-missing-imports --warn-unused-ignores
```

第 3 条需要 `mypy`（`typing_extensions` 由 typeshed 提供）。`tools/typecheck-consumer.py`
覆盖同步/异步/`requests` 门面、`iter_content` 的 `bytes`→`str` 重载、`session.stream` 的
两种上下文、WebSocket 收发，以及字段级推断（`cookies.get_dict()` 是 `dict[str, str]`、
`proxies` 是 `Mapping[str, str | None]`）。文件末尾两条**故意写错**的调用带
`# type: ignore[...]`，配 `--warn-unused-ignores` 就成了断言：哪天
`impersonate="chrome_200"` 或拼错的选项不再报错，ignore 变成多余，这一步立刻失败。
上面三条命令里 1、2 已进 CI 的 `check` 与 `python-regression` 任务。

`tools/audit-python-stubs.py` 断言四件事，任何一条不满足就以非零码退出（已接入
`.github/workflows/ci.yml` 的 `check` 任务）：

1. **覆盖**：`_python_impl/*.py` 里每个公开类、方法、函数在 `.pyi` 里要么声明为可调用，
   要么声明为同名字段；基类已声明的成员可以省略（例如 `AsyncResponse` 不重复 `__init__`）。
2. **参数**：运行时的每个参数名都出现在存根里，且共有参数的相对顺序一致（否则按位置调用
   会绑错参数）；默认值不一致直接报错，存根写 `...` 表示"不声明默认值"。
   `**kwargs` 只有两种合法写法：`Unpack[TypedDict]`，或把每个选项显式列为命名参数。
3. **文档**：每个声明的可调用对象必须有 docstring；带参数的还要有 `Args:` 段；有非 `None`
   返回值注解的建议带 `Returns:`/`Yields:` 段（缺失记为 warning）。
4. **再导出**：`_python_impl/<mod>.pyi` 的每个公开名字都能从 `chrome_client/<mod>` 与
   `chrome_client/requests/<mod>` 取到；包根要覆盖运行期 `__all__` 的全部名字；`py.typed`
   必须存在（否则类型检查器会直接忽略整套存根）。
5. **派生部分**：调用 `tools/generate-python-stubs.py --check`。它重算 profile `Literal`
   与全部门面存根，任何一处与运行时常量或 `_python_impl` 的公开名不一致都算失败。

## 维护约定

- **改运行时签名 = 改存根**。审计脚本会在 CI 里挡住漏改；本地先跑它再提交。
- **新增/上移 Chrome profile**：跑 `python3 tools/generate-python-stubs.py` 重写
  `impersonate.pyi` 的 `Literal` 块（幂等：已是最新时一个字节都不改），再更新
  `docs/COMPATIBILITY_BOUNDARY.md` 里的范围说明。
- **新增公开名字**：先加到 `_python_impl/<mod>.pyi`，再跑同一个生成器同步门面
  （`chrome_client/<mod>.pyi`、`chrome_client/requests/<mod>.pyi`、包根与
  `requests/__init__.pyi` 的再导出列表）。漏了会被审计脚本第 4、5 条同时拦下。
- **不要给存根加运行时语义**。存根里的 `...`、`TypedDict`、`Literal` 都只是给类型检查器看的；
  任何"顺手修一下运行时行为"的改动都不属于这个文件集。
- 已知边界：`_python_impl` 的私有类（`_SyncBodyReader`、`_AsyncState` 等）不建存根；
  native 扩展成员是 `Any`（精确描述它等于把 Rust 签名再抄一份）；`Literal` 不接受任意
  `str` 变量，需要动态 profile 时用 `cast(Impersonate, value)` 或 `# type: ignore`。
