#!/usr/bin/env python3
"""Checks that both Python extensions declare the surface the shared facade calls.

The two wheels -- cp39-abi3 (`chrome_client_native`, pyo3 0.30.1) and
cp36-abi3 (`chrome_client_native36`, pyo3 0.15.1) -- ship the *same* Python
package. Every name the facade reads therefore has to exist in both extensions,
and nothing checked that: a member added to one crate was a missing attribute on
the other, raised on the first call that needed it.

Two real defects came from exactly this, both shipped in 0.2.16:

  * ABI v14 added `PyEngine.platform_identity` to
    `bindings/python/src/lib.rs` only, and `_browser_headers` calls it on
    every request that names a profile: a session with `impersonate=` failed on
    the cp36 extension with AttributeError, wrapped into an UnsupportedFeature
    that blamed the Core.
  * `PyResponse` carried `reason_phrase`, `header_fields` and
    `http_version` as `#[pyo3(get)]` fields in the modern crate only. The
    facade reads them off the head object with getattr(..., None), so on the
    cp36 wheel Response.reason fell back to the status line and
    Response.http_version was None for every response.

What this gate reads, per crate:

  methods     fn <name> inside `impl PyEngine`, against every
              `PyEngine.<name>` used under bindings/python/chrome_client/
  attributes  `#[pyo3(get)]` struct fields and `#[getter]` methods, per
              `#[pyclass]` type, compared between the two crates
  module      `#[pyfunction]` names, module.add("...") attributes and
              module.add_class::<...>() classes -- the module-level surface,
              against every _native.<name> the facade reads. ABI v15 added
              header_flags for exactly one reason (the Core owns the header
              table, so the facade cannot keep a copy), and the facade's only
              way to reach it is this module-level name; the PyEngine scan above
              would never have noticed it going missing from one crate.

Exit 0 when each crate declares what the facade uses and the attribute sets of
the shared classes agree, 1 otherwise, naming the crate that is missing what.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FACADE = ROOT / "bindings" / "python" / "chrome_client"
CRATES = {
    "modern (chrome_client_native)": ROOT / "bindings" / "python" / "src" / "lib.rs",
    "legacy (chrome_client_native36)": ROOT / "bindings" / "python36" / "src" / "lib.rs",
}

_USE = re.compile(r"PyEngine\s*\.\s*([A-Za-z_]\w*)")
_GETTER = re.compile(r"#\[getter\]\s*\n\s*fn\s+([A-Za-z_]\w*)")
_PYCLASS = re.compile(r"#\[pyclass[^\]]*\]\s*\n\s*struct\s+(\w+)\s*\{(.*?)\n\}", re.S)
_PYO3_GET_FIELD = re.compile(r"#\[pyo3\(get\)\]\s*\n\s*(?:pub\s+)?([A-Za-z_]\w*)\s*:")
_IMPL = re.compile(r"impl\s+(\w+)\s*\{")
# The registration is what makes a function part of the module surface: a
# #[pyfunction] that is never registered compiles cleanly and is an
# AttributeError at the only place the facade can see it.
_PYFUNCTION = re.compile(r"wrap_pyfunction!\s*\(\s*([A-Za-z_]\w*)")
_MODULE_ATTRIBUTE = re.compile(r"module\s*\.\s*add\s*\(\s*.([A-Za-z_]\w*).")
_MODULE_CLASS = re.compile(r"add_class::<(\w+)>")


def used_by_facade():
    """Every `PyEngine.<name>` the facade reads, mapped to where it is read."""
    uses = {}
    for path in sorted(FACADE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in _USE.findall(text):
            uses.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return uses


def _block(text, open_brace):
    """The body of the brace block that starts at *open_brace*, plus its end."""
    depth, index = 1, open_brace + 1
    while depth and index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
        index += 1
    return text[open_brace + 1 : index], index


def module_surface(path):
    """The module-level names one crate exposes, as one set."""
    text = path.read_text(encoding="utf-8")
    return (
        set(_PYFUNCTION.findall(text))
        | set(_MODULE_ATTRIBUTE.findall(text))
        | set(_MODULE_CLASS.findall(text))
    )


def facade_module_uses():
    """Every _native.<name> the facade reads, mapped to where it is read.

    Parsed rather than matched: a regex over the same files also finds
    "_native.py" inside the docstring that names the loader module, and a gate
    that reports a file name as a missing attribute is a gate whose output has to
    be interpreted rather than read.
    """
    uses = {}
    for path in sorted(FACADE.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            base = node.value
            if isinstance(base, ast.Name) and base.id == "_native":
                uses.setdefault(node.attr, set()).add(str(path.relative_to(ROOT)))
    return uses


def declared_by_crate(path):
    """(PyEngine methods, {pyclass name: attribute names}) for one crate."""
    text = path.read_text(encoding="utf-8")
    if "impl PyEngine {" not in text:
        raise SystemExit("audit-binding-surface: no 'impl PyEngine {' in %s" % path)
    engine_block, _ = _block(text, text.index("impl PyEngine {"))
    # The block ends at the first line that closes it at column 1.
    end = engine_block.find("\n}\n")
    if end != -1:
        engine_block = engine_block[: end + 3]
    methods = set(re.findall(r"\bfn\s+([A-Za-z_]\w*)\s*[<(]", engine_block))

    attributes = {}
    for match in _PYCLASS.finditer(text):
        name, body = match.group(1), match.group(2)
        attributes.setdefault(name, set()).update(_PYO3_GET_FIELD.findall(body))
    for match in _IMPL.finditer(text):
        name = match.group(1)
        body, _end = _block(text, match.end() - 1)
        getters = set(_GETTER.findall(body))
        if getters:
            attributes.setdefault(name, set()).update(getters)
    return methods, attributes


def main():
    uses = used_by_facade()
    if not uses:
        print(
            "audit-binding-surface: the facade uses no PyEngine method at all; "
            "the scan is broken, not the code",
            file=sys.stderr,
        )
        return 1

    failed = False
    surfaces = {}
    for label, path in CRATES.items():
        methods, attributes = declared_by_crate(path)
        surfaces[label] = attributes
        missing = sorted(set(uses) - methods)
        if missing:
            failed = True
            print(
                "audit-binding-surface: %s does not declare %s"
                % (label, ", ".join(missing)),
                file=sys.stderr,
            )
            for name in missing:
                print(
                    "    %s is used at %s" % (name, ", ".join(sorted(uses[name]))),
                    file=sys.stderr,
                )
        else:
            print(
                "%s: declares all %d PyEngine method(s) the facade uses"
                % (label, len(uses))
            )

    # Module-level surface: the two crates ship one package, so a pyfunction
    # present in one and absent from the other is an AttributeError on that
    # interpreter and nowhere else.
    module_surfaces = {label: module_surface(path) for label, path in CRATES.items()}
    labels_by_module = list(module_surfaces)
    reference = module_surfaces[labels_by_module[0]]
    for other in labels_by_module[1:]:
        missing = sorted(reference - module_surfaces[other])
        extra = sorted(module_surfaces[other] - reference)
        if missing or extra:
            failed = True
            print(
                "audit-binding-surface: module surface differs -- %s has %s, "
                "%s has %s"
                % (labels_by_module[0], missing, other, extra),
                file=sys.stderr,
            )
    uses_module = facade_module_uses()
    for name, where in sorted(uses_module.items()):
        for label in labels_by_module:
            if name not in module_surfaces[label]:
                failed = True
                print(
                    "audit-binding-surface: %s does not declare _native.%s "
                    "(read at %s)" % (label, name, ", ".join(sorted(where))),
                    file=sys.stderr,
                )
    if not failed:
        print(
            "both extensions: module surface agrees on %s"
            % ", ".join(sorted(reference))
        )

    labels = list(surfaces)
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            for klass in sorted(set(surfaces[left]) & set(surfaces[right])):
                a = surfaces[left][klass]
                b = surfaces[right][klass]
                if a != b:
                    failed = True
                    print(
                        "audit-binding-surface: %s.%s differs -- %s has %s, %s has %s"
                        % (
                            klass,
                            "attributes",
                            left,
                            sorted(a),
                            right,
                            sorted(b),
                        ),
                        file=sys.stderr,
                    )
    if not failed:
        print(
            "%s: pyclass attribute sets agree for %s"
            % (
                "both extensions",
                ", ".join(sorted(set().union(*surfaces.values()))),
            )
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
