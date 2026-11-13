#!/usr/bin/env python3
"""Audit the Python type stubs against the runtime modules.

The stubs under ``bindings/python/chrome_client/**/*.pyi`` are hand-written, so
they can drift from the implementation they describe.  This script compares them
mechanically and fails on anything a type checker would silently paper over:

1. **Coverage** -- every public class, method and function in
   ``_python_impl/*.py`` is declared in the matching ``.pyi``, either as a
   callable or as an attribute, unless a stub base class already declares it.
2. **Parameters** -- every runtime parameter name survives in the stub, in the
   same relative order, with the same default where one is stated.  A stub may
   add named parameters only when the runtime forwards ``**kwargs`` or ``*args``
   (the verb methods document those options explicitly), and may always replace a
   default with ``...``.
3. **Documentation** -- every declared callable has a docstring, and a docstring
   for something with parameters contains an ``Args:`` block.
4. **Re-exports** -- every public name of an implementation module is reachable
   through the matching facade module (``chrome_client.models``,
   ``chrome_client.requests.models``, ...), and the package root re-exports
   everything the runtime ``__all__`` promises.

Usage::

    python3 tools/audit-python-stubs.py [--verbose]

Exit status is 0 when clean, 1 when any error was found.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from python_exe import interpreter

REPO = pathlib.Path(__file__).resolve().parents[1]
PY_PKG = REPO / "bindings" / "python" / "chrome_client"
IMPL = PY_PKG / "_python_impl"
GENERATOR = REPO / "tools" / "generate-python-stubs.py"
EXPANDER = REPO / "tools" / "expand-verb-signatures.py"

#: Modules whose facade file is a pure alias of the _python_impl module.
ALIASED = (
    "adapters",
    "api",
    "auth",
    "cookies",
    "engine",
    "exceptions",
    "impersonate",
    "models",
    "multipart",
    "sessions",
    "status_codes",
    "structures",
    "utils",
    "websockets",
)

#: Modules that have a runtime module and a stub, so they get the
#: class/method/parameter/docstring comparison. ALIASED above is the subset
#: that also has a facade module to re-export it. Auditing only ALIASED left
#: __init__ and _native -- the two stubs with no facade -- with no coverage
#: at all: a phantom function added to _native.pyi passed every check,
#: because no loop ever opened the file.
#: ``_native`` and ``__init__`` are the two stubs with no facade module.
SPECIAL = ("_native", "__init__")

#: Every private module that exists at runtime. Derived, not listed: a hardcoded
#: tuple meant a new module slipped in with no stub and no audit at all --
#: measured, adding ``_python_impl/_probe_new.py`` left this script reporting
#: "17 stub modules, 0 error(s)". ``browser_headers`` and ``client_hints`` were
#: in exactly that state: on disk, imported by ``sessions``, but absent from both
#: the tuple and the stub directory, so nothing compared them to anything.
#:
#: ``_types`` is a stub-only helper with no runtime module, so it is subtracted
#: rather than included.
IMPL_MODULES = tuple(sorted(
    path.stem for path in IMPL.glob("*.py")
    if path.stem != "__init__"))
STUB_ONLY = tuple(sorted(
    path.stem for path in IMPL.glob("*.pyi")
    if path.stem != "__init__" and not (IMPL / (path.stem + ".py")).exists()))

#: The tuple the comparison loops walk.
AUDITED = IMPL_MODULES


#: Runtime constants the stubs spell as literals.
CONSTANTS = {
    "DEFAULT_REDIRECT_LIMIT": "30",
    "DEFAULT_MAX_ENGINES": "8",
    "CONTENT_CHUNK_SIZE": "10240",
    "ITER_CHUNK_SIZE": "512",
    "OK": "1000",
}

#: Signatures whose extra stub parameters are inherited from a sibling module.
SKIP_MEMBERS = {"__init__": ()}

errors: list[str] = []
warnings: list[str] = []


def is_public(name: str) -> bool:
    """True for public names, including dunder methods."""
    if name.startswith("__") and name.endswith("__"):
        return True
    return not name.startswith("_")


def is_private_class(name: str) -> bool:
    """True for a class that is not part of the documented surface."""
    return name.startswith("_") and not name.startswith("__")


def docstring(node: ast.AST) -> str | None:
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        if isinstance(first.value.value, str):
            return first.value.value
    return None


def parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict]:
    """Flattens a signature into an ordered list of parameter descriptions."""
    args = node.args
    result: list[dict] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional, defaults):
        result.append({"name": arg.arg, "kind": "pos", "default": default})
    if args.vararg is not None:
        result.append({"name": args.vararg.arg, "kind": "vararg", "default": None})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        result.append({"name": arg.arg, "kind": "kwonly", "default": default})
    if args.kwarg is not None:
        result.append({"name": args.kwarg.arg, "kind": "kwarg", "default": None})
    return result


def default_source(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    text = ast.unparse(node)
    return CONSTANTS.get(text, text)


def is_overload(node: ast.AST) -> bool:
    """True for a ``@overload`` variant, which typeshed leaves undocumented."""
    for decorator in getattr(node, "decorator_list", []):
        if ast.unparse(decorator).endswith("overload"):
            return True
    return False


class Module:
    """A parsed stub or implementation, indexed for comparison."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.calls: dict[str, list[ast.AST]] = {}
        self.attrs: dict[str, set[str]] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.bases: dict[str, list[str]] = {}
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.calls.setdefault(node.name, []).append(node)
            elif isinstance(node, ast.ClassDef):
                self.classes[node.name] = node
                self.bases[node.name] = [
                    ast.unparse(base).split(".")[-1] for base in node.bases
                ]
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.calls.setdefault(f"{node.name}.{child.name}", []).append(child)
                    elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                        self.attrs.setdefault(node.name, set()).add(child.target.id)
                    elif isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name):
                                self.attrs.setdefault(node.name, set()).add(target.id)

    def primary(self, key: str) -> ast.AST | None:
        """The form that describes callers: the implementation, not an overload."""
        nodes = self.calls.get(key)
        if not nodes:
            return None
        concrete = [node for node in nodes if not is_overload(node)]
        return (concrete or nodes)[0]

    def documented(self, key: str) -> bool:
        nodes = [node for node in self.calls.get(key, []) if not is_overload(node)]
        return any(docstring(node) for node in (nodes or self.calls.get(key, [])))

    def declares(self, key: str) -> bool:
        if key in self.calls:
            return True
        owner, _, member = key.partition(".")
        return bool(owner) and member in self.attrs.get(owner, set())

    def inherits(self, class_name: str, member: str) -> bool:
        """Whether a base class in the same stub declares ``member``."""
        seen: set[str] = set()
        pending = list(self.bases.get(class_name, []))
        while pending:
            base = pending.pop()
            if base in seen or base not in self.classes:
                continue
            seen.add(base)
            if self.declares(f"{base}.{member}"):
                return True
            pending.extend(self.bases.get(base, []))
        return False

    def public_keys(self) -> list[str]:
        keys = []
        for key in self.calls:
            owner, _, member = key.partition(".")
            if owner in self.classes and is_private_class(owner):
                continue
            if member:
                if is_public(member):
                    keys.append(key)
            elif is_public(owner):
                keys.append(key)
        return keys


def check_parameters(label: str, impl: ast.AST, stub: ast.AST) -> None:
    impl_params = parameters(impl)  # type: ignore[arg-type]
    stub_params = parameters(stub)  # type: ignore[arg-type]
    impl_names = {param["name"] for param in impl_params}
    stub_names = {param["name"] for param in stub_params}
    accepts_extra = any(
        param["kind"] in ("vararg", "kwarg") for param in impl_params
    )
    impl_by_name = {param["name"]: param for param in impl_params}

    for param in impl_params:
        if param["kind"] in ("vararg", "kwarg"):
            continue
        if param["name"] not in stub_names:
            errors.append(f"{label}: runtime parameter {param['name']!r} is missing from the stub")

    for param in stub_params:
        if param["kind"] in ("vararg", "kwarg"):
            continue
        if param["name"] not in impl_names and not accepts_extra:
            errors.append(
                f"{label}: stub parameter {param['name']!r} does not exist at runtime "
                f"and the runtime does not forward **kwargs"
            )
        if param["name"] in impl_names:
            declared = default_source(param["default"])
            actual = default_source(impl_by_name[param["name"]]["default"])
            if declared is not None and declared != "..." and actual != declared:
                errors.append(
                    f"{label}: default for {param['name']!r} is {declared!r} in the stub "
                    f"but {actual!r} at runtime"
                )

    # Relative order of the shared parameters must be preserved: a caller relying
    # on positional arguments would otherwise bind the wrong one.
    shared_impl = [
        param["name"]
        for param in impl_params
        if param["name"] in stub_names and param["kind"] in ("pos", "kwonly")
    ]
    shared_stub = [
        param["name"]
        for param in stub_params
        if param["name"] in impl_names and param["kind"] in ("pos", "kwonly")
    ]
    if shared_impl and shared_stub and shared_impl != shared_stub:
        errors.append(f"{label}: parameter order differs between stub and runtime")

    if not any(param["kind"] == "kwarg" for param in stub_params) and any(
        param["kind"] == "kwarg" for param in impl_params
    ):
        # Two acceptable ways to describe a ``**kwargs`` carrier: accept them with
        # ``Unpack[TypedDict]``, or enumerate every option as a named parameter
        # (which is what the module-level ``request`` does, since it has to list
        # both the request options and the constructor-only ones).
        enumerated = [
            param
            for param in stub_params
            if param["kind"] in ("pos", "kwonly") and param["name"] not in impl_names
        ]
        if not enumerated:
            errors.append(f"{label}: runtime forwards **kwargs but the stub does not accept it")


def check_docstring(label: str, nodes: list[ast.AST], params: list[dict] | None = None) -> None:
    if not any(docstring(node) for node in nodes):
        errors.append(f"{label}: no docstring")
        return
    text = next(docstring(node) for node in nodes if docstring(node))
    if params is None:
        params = parameters(nodes[0])  # type: ignore[arg-type]
    meaningful = [param for param in params if param["name"] not in ("self", "cls")]
    if meaningful and "Args:" not in text:
        errors.append(f"{label}: docstring has no Args: block")
    returns = getattr(nodes[0], "returns", None)
    if returns is not None and ast.unparse(returns) not in ("None", "Never", "NoReturn"):
        if "Returns:" not in text and "Yields:" not in text:
            warnings.append(f"{label}: docstring has no Returns: block")


def check_module(module: str, verbose: bool) -> None:
    impl_path = IMPL / f"{module}.py"
    stub_path = IMPL / f"{module}.pyi"
    if not stub_path.exists():
        errors.append(f"_python_impl/{module}.pyi: missing stub for {impl_path.name}")
        return

    impl, stub = Module(impl_path), Module(stub_path)

    for name in impl.classes:
        if not is_public(name):
            continue
        if name not in stub.classes:
            errors.append(f"_python_impl/{module}.pyi: class {name} is not declared")
            continue
        check_docstring(f"_python_impl/{module}.pyi: class {name}", [stub.classes[name]], params=[])

    for key in impl.public_keys():
        owner, _, member = key.partition(".")
        if not stub.declares(key):
            if member and stub.inherits(owner, member):
                continue
            if member == "__init__" and owner in stub.classes:
                continue
            errors.append(f"_python_impl/{module}.pyi: {key} is not declared")
            continue
        if not stub.declares(key) or key not in stub.calls:
            # Declared as an attribute; nothing further to compare.
            continue
        nodes = [node for node in stub.calls[key] if not is_overload(node)] or stub.calls[key]
        check_docstring(f"_python_impl/{module}.pyi: {key}", nodes)
        check_parameters(f"_python_impl/{module}.pyi: {key}", impl.primary(key), nodes[0])

    for key in stub.public_keys():
        if key in impl.calls or impl.declares(key):
            continue
        owner, _, member = key.partition(".")
        # A module-level name has no dot, so member is empty. Skipping those
        # ("or not member") exempted every module-level function from this
        # check: injecting "def totally_phantom_function() -> int: ..." into
        # _native.pyi passed with 0 errors. SKIP_MEMBERS is keyed by member
        # name and only holds dunders, which always come with an owner, so the
        # guard must require member to be non-empty rather than its absence.
        if member and member in SKIP_MEMBERS.get(member, ()):
            continue
        if owner and impl.inherits(owner, member):
            continue
        # fail-closed: a stub that declares a member the runtime does not have
        # promises the caller an attribute that raises AttributeError. A warning
        # here let nine phantom members survive with every gate green, and the
        # only reader who ever saw it was the one who ran the audit by hand.
        errors.append(
            f"_python_impl/{module}.pyi: {key} has no runtime counterpart; the "
            f"stub promises an attribute that raises AttributeError"
        )

    if verbose:
        print(f"  {module}: {len(impl.public_keys())} runtime callables checked")


def public_names(path: pathlib.Path) -> set[str]:
    """Names a module exposes: definitions plus PEP 484 re-exports."""
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if is_public(node.name):
                names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if is_public(node.target.id):
                names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and is_public(target.id):
                    names.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname == alias.name and is_public(alias.name):
                    names.add(alias.name)
    return names


def check_facade(verbose: bool) -> None:
    impl_init = public_names(IMPL / "__init__.py")

    for module in ALIASED:
        expected = public_names(IMPL / f"{module}.pyi")
        for facade in (PY_PKG / f"{module}.pyi", PY_PKG / "requests" / f"{module}.pyi"):
            if not facade.exists():
                errors.append(f"{facade.relative_to(REPO)}: missing")
                continue
            missing = sorted(expected - public_names(facade))
            if missing:
                errors.append(f"{facade.relative_to(REPO)}: does not re-export {', '.join(missing)}")
            if verbose:
                print(f"  {facade.relative_to(PY_PKG)}: {len(expected)} names")

    declared_root = public_names(PY_PKG / "__init__.pyi")
    for module in ALIASED:
        if module not in declared_root:
            errors.append(f"chrome_client/__init__.pyi: submodule {module} is not re-exported")
    missing = sorted(impl_init - declared_root)
    if missing:
        errors.append(f"chrome_client/__init__.pyi: missing {', '.join(missing)}")

    requests_stub = public_names(PY_PKG / "requests" / "__init__.pyi")
    missing = sorted(impl_init - requests_stub)
    if missing:
        errors.append(f"chrome_client/requests/__init__.pyi: missing {', '.join(missing)}")

    if not (PY_PKG / "py.typed").exists():
        errors.append("chrome_client/py.typed: missing, so type checkers will ignore the stubs")


def delegation_failure(tool: str, result: subprocess.CompletedProcess) -> str:
    """The error a delegated check must produce when its output is unusable.

    A non-zero exit whose output matched no known prefix used to append nothing
    at all, so a crashed generator or expander read as a clean audit. The exit
    status and the output tail are the only facts available, so report those.
    """
    combined = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    tail = "\n".join(combined.splitlines()[-5:]) or "<no output>"
    return (
        "%s --check exited %d but its output matched no known prefix, so the "
        "check could not be evaluated; that is a failure, not a pass. Output "
        "tail: %s" % (tool, result.returncode, tail)
    )


def parse_delegated_output(
    tool: str,
    result: subprocess.CompletedProcess,
    stale_prefix: str,
    rerun_prefix: str,
    stale_label: str,
) -> None:
    """Collect errors from a delegated check, reading BOTH streams.

    Reading one stream is how these two checks silently passed: the generator
    writes to stderr and the expander to stdout, so a traceback landing on the
    other stream left zero errors behind a non-zero exit. Every non-empty line
    of either stream is consumed, and an exit with nothing recognisable becomes
    one fallback error instead of an empty error list.
    """
    recognised = 0
    unmatched: list[str] = []
    for stream in (result.stdout, result.stderr):
        for line in stream.strip().splitlines():
            stripped = line.strip()
            if stripped.startswith(stale_prefix):
                errors.append(stale_label % stripped[len(stale_prefix) :])
                recognised += 1
            elif stripped.startswith(rerun_prefix) or not stripped:
                continue
            else:
                unmatched.append(stripped)
    if recognised == 0:
        errors.append(delegation_failure(tool, result))
    else:
        errors.extend(unmatched)


def runtime_names(path: pathlib.Path) -> set[str]:
    """Every name the runtime module binds at import time.

    Read with ``ast`` rather than by importing: the package pulls in the native
    extension, which needs the Core shared library on the loader path. Imports
    inside ``if``/``try`` blocks are included because those are the conditional
    definitions (a ``TYPE_CHECKING`` block is not, since its body is not part of
    a real import, but nothing in ``_python_impl`` uses one).
    """
    names: set[str] = set()

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.If, ast.Try)):
                walk(node.body)
                walk(node.orelse)
                walk(getattr(node, "finalbody", []))

    walk(ast.parse(path.read_text(encoding="utf-8")).body)
    return names


def type_alias_names(path: pathlib.Path) -> set[str]:
    """Names a stub declares as ``TypeAlias``, which exist only at type-check time.

    ``AuthCredentials: TypeAlias = Tuple[...]`` is a statement to a checker and
    nothing at all to CPython, so it is legitimately absent from the runtime. The
    facade must still re-export it for the checker, which is why this is an
    exemption from the runtime-existence rule rather than a violation of it.
    """
    aliases: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if ast.unparse(node.annotation).endswith("TypeAlias"):
                aliases.add(node.target.id)
    return aliases


def check_facade_runtime_names(verbose: bool) -> None:
    """A facade re-export must name something the runtime module actually binds.

    The facades are generated from the *implementation stubs*, so a name that
    exists only in ``_python_impl/X.pyi`` -- a type alias, or a type that was
    removed from the runtime but kept in the stub -- is re-exported by
    ``chrome_client/X.pyi`` as if it were importable. At runtime the facade is
    the ``_python_impl`` module itself, so ``from chrome_client.models import
    RawHeaderBlock`` raises ImportError on a name the stub advertises. Type
    aliases are exempt (they are not importable by design); everything else must
    be bound by the runtime module.
    """
    checked = 0
    aliases_exempt = 0
    for module in ALIASED:
        runtime = runtime_names(IMPL / f"{module}.py")
        aliases = type_alias_names(IMPL / f"{module}.pyi")
        for facade in (PY_PKG / f"{module}.pyi", PY_PKG / "requests" / f"{module}.pyi"):
            if not facade.exists():
                continue
            for node in ast.parse(facade.read_text(encoding="utf-8")).body:
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.endswith(f"_python_impl.{module}"):
                    continue
                for alias in node.names:
                    checked += 1
                    if alias.name in runtime:
                        continue
                    if alias.name in aliases:
                        aliases_exempt += 1
                        continue
                    errors.append(
                        f"{facade.relative_to(REPO)}: re-exports {alias.name}, but "
                        f"_python_impl/{module}.py does not bind it, so the import "
                        "raises ImportError on a name the stub advertises"
                    )
    if verbose:
        print(
            "  facade re-exports: %d names checked against the runtime "
            "(%d type aliases exempt)" % (checked, aliases_exempt)
        )

def check_generated(verbose: bool) -> None:
    """The derived stub parts must match what the generator would write.

    Delegated so there is a single definition of "expected". This is what catches
    a bumped Chrome major whose profile ``Literal`` was not regenerated, and a
    public name added to an implementation stub whose facade re-export list was
    not, while the more specific checks above still catch the reverse drift.
    """
    result = subprocess.run(
        [interpreter(), str(GENERATOR), "--check"], capture_output=True, text=True
    )
    if result.returncode != 0:
        parse_delegated_output(
            "tools/generate-python-stubs.py",
            result,
            "stale: ",
            "run ",
            "stale generated stub: %s",
        )
    elif verbose:
        print("  generated stubs: facade re-exports and profile Literals are current")


def check_verb_signatures(verbose: bool) -> None:
    """Every verb method must list the same options as the ``request()`` it forwards to.

    ``**kwargs: Unpack[TypedDict]`` (PEP 692) is only expanded by some IDEs --
    PyCharm shows nothing but the explicit ``url``/``params`` -- so the options are
    written out as named parameters instead. That duplicates the list across the
    29 verb methods plus the two ``send()``s, and this is what keeps the copies
    in step with ``request()``. Run ``tools/expand-verb-signatures.py`` to rewrite
    them.
    """
    result = subprocess.run(
        [interpreter(), str(EXPANDER), "--check"], capture_output=True, text=True
    )
    if result.returncode != 0:
        parse_delegated_output(
            "tools/expand-verb-signatures.py",
            result,
            "stale: ",
            "run:",
            "verb signature out of date: %s",
        )
    elif verbose:
        print("  verb signatures: every parameter list matches its request()")


def check_profile_annotations(verbose: bool) -> None:
    """Attributes holding a profile name must say *which* profiles, not just ``str``.

    ``Session.impersonate`` was annotated ``Optional[str]``, so hovering it showed
    ``str | None`` and the caller could not see that ``chrome_99``..``chrome_155``
    are the accepted values -- the Literal lists existed but were only reachable
    through the ``impersonate=`` parameter. A bare ``str`` here also silently
    accepts any typo at type-check time while the runtime raises
    ``ImpersonateError``, which is the opposite of the fail-closed contract.

    The alias must additionally be *imported* by the stub that uses it: an
    unimported name resolves to ``Any`` in a checker, which is worse than
    ``str`` because it also disables every other check on the attribute.
    """
    wanted = {
        "impersonate": "ChromeProfileName",
        "http_version": "HttpVersionNormalized",
    }
    for module, aliases in (("sessions", wanted), ("engine", {"impersonate": "ChromeProfileName"})):
        path = IMPL / f"{module}.pyi"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            expected = aliases.get(node.target.id)
            if expected is None:
                continue
            text = ast.unparse(node.annotation)
            if expected not in text:
                errors.append(
                    "%s.pyi: %s is annotated %r; use %s so the accepted profiles "
                    "are visible on hover" % (module, node.target.id, text, expected))
            elif expected not in imported:
                errors.append(
                    "%s.pyi: %s uses %s but does not import it, so a checker "
                    "resolves the attribute to Any" % (module, node.target.id, expected))
    if verbose:
        print("  profile annotations: attributes name the profile Literal")


def pinned_profile_range() -> tuple[int, int]:
    """Returns ``(oldest, latest)`` read from the runtime, not restated here.

    Parsed with ``ast`` rather than imported so the audit does not need the
    native extension loaded to know the bounds.
    """
    tree = ast.parse((IMPL / "impersonate.py").read_text(encoding="utf-8"))
    constants: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ("OLDEST_CHROME", "LATEST_CHROME"):
                constants[target.id] = ast.literal_eval(node.value)
    missing = {"OLDEST_CHROME", "LATEST_CHROME"} - set(constants)
    if missing:
        raise SystemExit("could not read %s from impersonate.py" % ", ".join(sorted(missing)))
    return constants["OLDEST_CHROME"], constants["LATEST_CHROME"]


def check_prose_version_ranges(verbose: bool) -> None:
    """Version ranges written in stub prose must match the pinned profile range.

    The Literal blocks are generated, so a bumped Chrome major cannot drift
    there -- but the prose around them is hand-written and nothing regenerates
    it.  ``chrome_99 .. chrome_153`` survived two profile additions (154 and
    155) in nine places across three stubs, including the docstring of
    ``available_profiles()``, whose example return value was simply wrong.

    Only prose is checked, never the Literal blocks: a Literal is a list of
    names, not a range, and the generator already owns it.
    """
    oldest, latest = pinned_profile_range()
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(IMPL.glob("*.pyi"))
    )
    # ``chrome_99`` .. ``chrome_155``, ``chrome99``..``chrome155``,
    # ``["chrome_99", "chrome_100", ..., "chrome_153"]`` and the en-dash/ellipsis
    # spellings all reduce to: two version numbers separated by a range token.
    pattern = re.compile(
        r"chrome_?(?P<low>\d+)\b[^\n]{0,24}?(?:\.\.\.?|…|--|\bto\b)"
        r"[^\n]{0,24}?chrome_?(?P<high>\d+)"
    )
    found = 0
    for match in pattern.finditer(text):
        found += 1
        low, high = int(match.group("low")), int(match.group("high"))
        if (low, high) != (oldest, latest):
            errors.append(
                "stub prose states a Chrome profile range of %d..%d, but the "
                "pinned range is %d..%d (offending text: %r)"
                % (low, high, oldest, latest, match.group(0).strip())
            )
    if not found:
        errors.append(
            "no Chrome profile range found in the stub prose; either every "
            "range was deleted or the pattern no longer matches the spelling "
            "used, and this check would then pass vacuously"
        )
    if verbose:
        print("  prose version ranges: %d checked against %d..%d" % (found, oldest, latest))


def stub_defined_names() -> set[str]:
    """Every name any stub defines, imports or re-exports."""
    names: set[str] = set()
    for path in sorted(IMPL.rglob("*.pyi")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
    return names


def check_prose_type_references(verbose: bool) -> None:
    """Every type named in stub prose must actually exist somewhere.

    Sphinx-style cross-references are how these stubs point at each other, and a
    reference to a name that no longer exists renders as plain text -- the reader
    gets no link and no error, so the rot is invisible.  A ``:class:`` pointing
    at ``ChromeFamilyAlias`` would have survived that type's removal without this
    check.

    Only the *bare* form is verified against the stub namespace.  A qualified
    reference such as ``chrome_client.exceptions.UnsupportedFeature`` is resolved
    through the runtime package instead, and external roots (``json.loads``) are
    not this project's to verify.  ``:attr:`` is deliberately excluded: it names
    a member of the enclosing scope, not a module-level type.
    """
    defined = stub_defined_names()
    pattern = re.compile(r":(class|data|func|exc|mod):`~?([A-Za-z_][A-Za-z0-9_.]*)`")
    #: Modules that exist as packages, so ``:mod:`chrome_client``` is a real
    #: target even though no stub *defines* it as a name.
    modules = {"chrome_client"}
    checked = 0
    for path in sorted(IMPL.rglob("*.pyi")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            role, name = match.group(1), match.group(2)
            if "." in name:
                continue
            checked += 1
            if name in defined or hasattr(builtins, name):
                continue
            if role == "mod" and name in modules:
                continue
            errors.append(
                "%s: prose references %r, which no stub defines and no builtin "
                "provides; the cross-reference cannot resolve"
                % (path.relative_to(REPO), name)
            )
    if not checked:
        errors.append(
            "no bare cross-references found in the stub prose; either they were "
            "all removed or the pattern no longer matches, and this check would "
            "then pass vacuously"
        )
    if verbose:
        print("  prose type references: %d bare references resolved" % checked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="print per-module counts")
    args = parser.parse_args()

    print("Auditing Python type stubs")
    for module in AUDITED:
        check_module(module, args.verbose)
    check_facade(args.verbose)
    check_facade_runtime_names(args.verbose)
    check_generated(args.verbose)
    check_verb_signatures(args.verbose)
    check_profile_annotations(args.verbose)
    check_prose_version_ranges(args.verbose)
    check_prose_type_references(args.verbose)

    total = sum(1 for _ in IMPL.glob("*.pyi"))
    for message in warnings:
        print(f"warning: {message}")
    for message in errors:
        print(f"error: {message}")
    print(f"\n{total} stub modules, {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
