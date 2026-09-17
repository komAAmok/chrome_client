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
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
PY_PKG = REPO / "bindings" / "python" / "chrome_client"
IMPL = PY_PKG / "_python_impl"
GENERATOR = REPO / "tools" / "generate-python-stubs.py"

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
        if member in SKIP_MEMBERS.get(member, ()) or not member:
            continue
        if owner and impl.inherits(owner, member):
            continue
        warnings.append(f"_python_impl/{module}.pyi: {key} has no runtime counterpart")

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


def check_generated(verbose: bool) -> None:
    """The derived stub parts must match what the generator would write.

    Delegated so there is a single definition of "expected". This is what catches
    a bumped Chrome major whose profile ``Literal`` was not regenerated, and a
    public name added to an implementation stub whose facade re-export list was
    not, while the more specific checks above still catch the reverse drift.
    """
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True
    )
    if result.returncode != 0:
        for line in result.stderr.strip().splitlines():
            if line.startswith("stale: "):
                errors.append("stale generated stub: %s" % line[len("stale: ") :])
            elif line.startswith("run "):
                continue
            else:
                errors.append(line)
    elif verbose:
        print("  generated stubs: facade re-exports and profile Literals are current")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verbose", action="store_true", help="print per-module counts")
    args = parser.parse_args()

    print("Auditing Python type stubs")
    for module in ALIASED:
        check_module(module, args.verbose)
    check_facade(args.verbose)
    check_generated(args.verbose)

    total = sum(1 for _ in IMPL.glob("*.pyi"))
    for message in warnings:
        print(f"warning: {message}")
    for message in errors:
        print(f"error: {message}")
    print(f"\n{total} stub modules, {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
