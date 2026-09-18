#!/usr/bin/env python3
"""把 verb 方法的参数表与 ``request()`` 对齐。

每个 verb 方法（``api.get``、``Session.post``、``AsyncSession.delete`` …）的参数
集合必须与它转发的目标一致：

* 模块级 ``api.*`` 转发到 ``api.request``，因此还额外接受 ``base_url`` 等
  10 个「构造期选项」（运行期用一个临时会话发这一次请求）。
* ``Session.*`` / ``AsyncSession.*`` 转发到 ``Session.request``，没有那 10 个。
* ``session()`` / ``async_session()`` 转发到 ``BaseSession.__init__``。
* ``Session.stream(method, url, ...)`` 转发到 ``Session.request``，但 ``stream``
  被强制打开，所以不出现。

为什么不写成 ``**kwargs: Unpack[RequestOptions]``：PEP 692 的 Unpack 只有部分
IDE 支持，PyCharm 只会显示显式的 ``url``/``params``。写成具名参数后，任何
编辑器都能补全全部选项。

只替换「参数表」和 docstring 里的 ``Args:`` 段，摘要/Returns/Raises/Example
逐字保留。幂等：``--check`` 用于 CI 断言，无参数则原地重写。

用法::

    python3 tools/expand-verb-signatures.py --check   # CI：不一致就退出 1
    python3 tools/expand-verb-signatures.py           # 重写存根
"""

import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
IMPL = REPO / "bindings" / "python" / "chrome_client" / "_python_impl"
API_PYI = IMPL / "api.pyi"
SESS_PYI = IMPL / "sessions.pyi"

SECTIONS = ("Returns:", "Raises:", "Example:", "Note:", "Yields:")

# 每个 verb 的「惯用位置参数」，与 requests 的调用习惯一致。
LEADING_API = {
    "get": ["url", "params"],
    "options": ["url"],
    "head": ["url"],
    "post": ["url", "data", "json"],
    "put": ["url", "data"],
    "patch": ["url", "data"],
    "delete": ["url"],
    "trace": ["url"],
    "query": ["url"],
}
# Session 级与模块级一致：put/patch 只有 url/data 是位置参数，json 走关键字。
# （运行期 ``put(self, url, data=None, **kwargs)`` 也没有 json 位置参数，requests 的
# ``put`` 同样只有 ``data``。）
LEADING_SESS = dict(LEADING_API)

# head 在运行期强制 allow_redirects=False
OVERRIDE = {"head": {"allow_redirects": "False"}}

WIDTH = 88


# --------------------------------------------------------------- 读取

def param_spec(node, drop=()):
    a = node.args
    positional = list(a.posonlyargs) + list(a.args)
    defaults = list(a.defaults)
    offset = len(positional) - len(defaults)
    out = []
    for index, arg in enumerate(positional):
        default = ast.unparse(defaults[index - offset]) if index >= offset else None
        out.append((arg.arg, ast.unparse(arg.annotation) if arg.annotation else None, default))
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        out.append((arg.arg, ast.unparse(arg.annotation) if arg.annotation else None,
                    ast.unparse(default) if default else None))
    return [p for p in out if p[0] not in drop]


def raw_doc(node):
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        return node.body[0].value.value
    return None


def doc_args(raw):
    lines = raw.split("\n")
    at = next((i for i, line in enumerate(lines) if line.strip() == "Args:"), None)
    if at is None:
        return {}
    base = len(lines[at]) - len(lines[at].lstrip())
    entries, name = {}, None
    for line in lines[at + 1:]:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base and stripped.endswith(":"):
            break
        if indent == base + 4 and ":" in stripped and not stripped.startswith("`"):
            candidate = stripped.split(":", 1)[0].strip().replace("*", "")
            if candidate.replace("_", "").isalnum():
                name = candidate
                entries[name] = stripped.split(":", 1)[1].strip()
                continue
        if name is not None:
            entries[name] = (entries[name] + " " + stripped).strip()
    return entries


def find(tree, name, owner=None):
    for node in tree.body:
        if owner is None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        elif isinstance(node, ast.ClassDef) and node.name == owner:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    return child
    raise SystemExit("not found in stub: %s.%s" % (owner, name))


def find_all(tree, name, owner):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == owner:
            return [c for c in node.body
                    if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef)) and c.name == name]
    return []


# --------------------------------------------------------------- 渲染

def render_signature(indent, prefix, name, params, returns):
    body = []
    for pname, annotation, default in params:
        text = pname if annotation is None else "%s: %s" % (pname, annotation)
        if default is not None:
            text += " = %s" % default
        body.append("%s    %s," % (indent, text))
    return ["%s%s%s(" % (indent, prefix, name)] + body + ["%s) -> %s:" % (indent, returns)]


def render_args(params, docs, indent):
    out = ["%sArgs:" % indent]
    for name, _ann, _default in params:
        doc = docs.get(name, "")
        lead = "%s    %s:" % (indent, name)
        if not doc:
            out.append(lead)
            continue
        room = max(WIDTH - len(lead) - 1, 20)
        rows, row = [], ""
        for word in doc.split():
            candidate = word if not row else row + " " + word
            if len(candidate) <= room:
                row = candidate
            else:
                rows.append(row)
                row = word
        rows.append(row)
        out.append("%s %s" % (lead, rows[0]))
        for extra in rows[1:]:
            out.append("%s        %s" % (indent, extra))
    return out


def rebuild_docstring(raw, new_args):
    lines = raw.split("\n")
    at = next((i for i, line in enumerate(lines) if line.strip() == "Args:"), None)
    if at is None:
        return lines + [""] + new_args
    base = len(lines[at]) - len(lines[at].lstrip())
    end = len(lines)
    for i in range(at + 1, len(lines)):
        stripped = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        if stripped and indent <= base and stripped.startswith(SECTIONS):
            end = i
            break
    head = lines[:at]
    while head and not head[-1].strip():
        head.pop()
    return head + [""] + new_args + [""] + lines[end:]


def expected_block(node, prefix, verb, sig_params, docs, returns):
    indent = " " * node.col_offset
    body_indent = " " * node.body[0].col_offset
    raw = raw_doc(node)
    if raw is None:
        return None
    doc_lines = raw.split("\n")
    args_line = next(line for line in doc_lines if line.strip() == "Args:")
    args_indent = " " * (len(args_line) - len(args_line.lstrip()))
    # Args 覆盖签名里除 self 之外的每个参数（含 __call__ 的 method/url）
    documented = [p for p in sig_params if p[0] != "self"]
    rebuilt = rebuild_docstring(raw, render_args(documented, docs, args_indent))
    # 原始 docstring 的最后一项是三引号前的缩进；它不能自成一个空行，
    # 否则每跑一次就多一个空行（不幂等）。
    while rebuilt and not rebuilt[-1].strip():
        rebuilt.pop()
    block = render_signature(indent, prefix, verb, sig_params, returns)
    block.append('%s"""%s' % (body_indent, rebuilt[0]))
    block.extend(rebuilt[1:])
    block.append('%s"""' % body_indent)
    block.append("%s..." % body_indent)
    return block


def build_params(verb, spec, leading):
    lookup = {p[0]: p for p in spec}
    chosen = [lookup[name] for name in leading if name in lookup]
    taken = set(leading) | {"method", "self"}
    chosen += [p for p in spec if p[0] not in taken]
    override = OVERRIDE.get(verb, {})
    return [(n, a, override.get(n, d)) for n, a, d in chosen]


# --------------------------------------------------------------- 主流程

def process(path, jobs, check, stale):
    """``jobs``: [(owner, verb, prefix, returns, spec, docs, leading, lead_sig)]"""
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.split("\n")
    # 从后往前替换，避免行号失效
    planned = []
    for owner, verb, prefix, returns, spec, docs, leading, lead_sig in jobs:
        nodes = find_all(tree, verb, owner) if owner else [find(tree, verb)]
        for node in nodes:
            params = build_params(verb, spec, leading)
            sig_params = ([("self", None, None)] + params) if owner else params
            if lead_sig:
                sig_params = lead_sig + params
            block = expected_block(node, prefix, verb, sig_params, docs, returns)
            if block is None:
                continue
            current = lines[node.lineno - 1:node.end_lineno]
            if current != block:  # noqa: SIM108 - keep the early-exit readable
                label = "%s.%s" % (owner, verb) if owner else verb
                stale.append("%s (%s)" % (label, path.name))
                planned.append((node.lineno - 1, node.end_lineno, block))
    if check or not planned:
        return
    for start, end, block in sorted(planned, reverse=True):
        lines[start:end] = block
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    check = "--check" in sys.argv

    api_tree = ast.parse(API_PYI.read_text(encoding="utf-8"))
    sess_tree = ast.parse(SESS_PYI.read_text(encoding="utf-8"))

    api_req = find(api_tree, "request")
    sess_req = find(sess_tree, "request", "Session")
    base_init = find(sess_tree, "__init__", "BaseSession")

    api_spec = param_spec(api_req)
    sess_spec = param_spec(sess_req)
    init_spec = param_spec(base_init, drop=("self",))
    api_docs = doc_args(raw_doc(api_req))
    sess_docs = doc_args(raw_doc(sess_req))
    init_docs = doc_args(raw_doc(base_init))

    stream_spec = param_spec(sess_req, drop=("self", "method", "url", "stream"))
    stream_lead = [("self", None, None), ("method", "str", None), ("url", "str", None)]

    # send() 的 options 是 request() 参数的子集：body/header/params 已含在 PreparedRequest
    # 里，所以只剩传输与重定向相关的键，另加两个 send 特有的重定向控制键。
    SEND_KEYS = ["timeout", "proxies", "stream", "verify", "cert", "impersonate",
                 "proxy", "http_version", "max_redirects", "max_response_bytes",
                 "default_encoding", "discard_cookies", "retry", "cache_mode",
                 "priority", "content_callback", "raise_for_status"]
    send_spec = [p for p in sess_spec if p[0] in SEND_KEYS]
    send_spec += [("native_redirects", "Optional[bool]", "None"),
                  ("python_redirects", "Optional[bool]", "None")]
    send_docs = dict(sess_docs)
    send_docs["request"] = ("The :class:`~chrome_client.PreparedRequest` to send. A request "
                            "whose URL matches a non-default adapter is delegated to that adapter.")
    send_docs["native_redirects"] = ("``True`` lets Chromium follow redirects internally "
                                     "(the default).")
    send_docs["python_redirects"] = ("``True`` follows redirects from Python instead, honouring "
                                     "``max_redirects``.")
    send_lead = [("self", None, None), ("request", "PreparedRequest", None)]

    api_jobs = [(None, verb, "def ", "Response", api_spec, api_docs, leading, None)
                for verb, leading in LEADING_API.items()]
    api_jobs.append((None, "async_session", "def ", "AsyncSession",
                     init_spec, init_docs, [p[0] for p in init_spec], None))
    api_jobs.append((None, "session", "def ", "Session",
                     init_spec, init_docs, [p[0] for p in init_spec], None))

    sess_jobs = []
    for owner, prefix, returns in (("Session", "def ", "Response"),
                                  ("AsyncSession", "async def ", "AsyncResponse")):
        sess_jobs += [(owner, verb, prefix, returns, sess_spec, sess_docs, leading, None)
                      for verb, leading in LEADING_SESS.items()]
    # Session.stream(method, url, ...) / AsyncSession.stream(...) 的 __call__：
    # 目标类名是 _StreamFlag / _AsyncStreamFlag，不是 Session。
    stream_leading = [p[0] for p in stream_spec]
    sess_jobs.append(("_StreamFlag", "__call__", "def ", '"_StreamContext"',
                      stream_spec, sess_docs, stream_leading, stream_lead))
    sess_jobs.append(("_AsyncStreamFlag", "__call__", "def ", '"_AsyncStreamContext"',
                      stream_spec, sess_docs, stream_leading, stream_lead))
    sess_jobs.append(("Session", "send", "def ", "Response",
                      send_spec, send_docs, [], send_lead))
    sess_jobs.append(("AsyncSession", "send", "async def ", "AsyncResponse",
                      send_spec, send_docs, [], send_lead))

    stale = []
    process(API_PYI, api_jobs, check, stale)
    process(SESS_PYI, sess_jobs, check, stale)

    if check:
        if stale:
            print("verb 参数表与 request() 不一致，需要重跑本工具：")
            for item in stale:
                print("  stale: %s" % item)
            print("run: python3 tools/expand-verb-signatures.py")
            return 1
        print("verb 参数表与 request() 一致（api 9 + session 20 + 4 处）")
        return 0

    print("已重写 %d 处 verb 签名" % len(stale) if stale else "verb 签名已是最新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
