#!/usr/bin/env python3
"""Prepare an xwin /winsysroot for Chromium clang-cl cross builds."""

import json
import sys
from pathlib import Path


SDK_VERSION = "10.0.28000"
CHROMIUM_SDK_VERSION = f"{SDK_VERSION}.0"
MSVC_VERSION = "14.51"


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    if len(sys.argv) != 3:
        fail(f"usage: {Path(sys.argv[0]).name} XWIN_WINSYSROOT CHROMIUM_SRC")

    root = Path(sys.argv[1]).resolve()
    chromium_src = Path(sys.argv[2]).resolve()
    sdk = root / "Windows Kits" / "10"
    msvc = root / "VC" / "Tools" / "MSVC" / MSVC_VERSION
    required = (
        sdk / "Include" / SDK_VERSION / "um" / "windows.h",
        sdk / "Lib" / SDK_VERSION / "um" / "x64" / "kernel32.lib",
        sdk / "Lib" / SDK_VERSION / "um" / "x86" / "kernel32.lib",
        sdk / "Lib" / SDK_VERSION / "um" / "arm64" / "kernel32.lib",
        msvc / "include" / "vcruntime.h",
        msvc / "include" / "atldef.h",
        msvc / "lib" / "x64" / "libcmt.lib",
        msvc / "lib" / "x86" / "libcmt.lib",
        msvc / "lib" / "arm64" / "libcmt.lib",
        chromium_src
        / "third_party/llvm-build/Release+Asserts/lib/clang/23/lib/windows"
        / "clang_rt.builtins-x86_64.lib",
        chromium_src
        / "third_party/llvm-build/Release+Asserts/lib/clang/23/lib/windows"
        / "clang_rt.builtins-i386.lib",
        chromium_src
        / "third_party/llvm-build/Release+Asserts/lib/clang/23/lib/windows"
        / "clang_rt.builtins-aarch64.lib",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        fail("xwin winsysroot is incomplete:\n" + "\n".join(missing))

    include = [
        ["Windows Kits", "10", "Include", CHROMIUM_SDK_VERSION, part]
        for part in ("um", "shared", "winrt", "ucrt")
    ]
    include.append(["VC", "Tools", "MSVC", MSVC_VERSION, "include"])
    common = {
        "VSINSTALLDIR": [["."]],
        "VCINSTALLDIR": [["VC"]],
        "VCToolsInstallDir": [["VC", "Tools", "MSVC", MSVC_VERSION]],
        "INCLUDE": include,
        "LIBPATH": [],
        "PATH": [],
    }
    bin_dir = sdk / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for cpu in ("x86", "x64", "arm64"):
        env = dict(common)
        env["LIB"] = [
            ["VC", "Tools", "MSVC", MSVC_VERSION, "lib", cpu],
            ["Windows Kits", "10", "Lib", SDK_VERSION, "um", cpu],
            ["Windows Kits", "10", "Lib", SDK_VERSION, "ucrt", cpu],
        ]
        output = bin_dir / f"SetEnv.{cpu}.json"
        content = json.dumps({"env": env}, indent=2) + "\n"
        if not output.exists() or output.read_text() != content:
            output.write_text(content)

    # Chromium's pinned SDK identifier has a fourth component; xwin's layout
    # omits it. Clang discovers either layout, while vs_toolchain.py needs this
    # alias for its SDK capability check.
    include_alias = sdk / "Include" / CHROMIUM_SDK_VERSION
    if not include_alias.exists():
        include_alias.symlink_to(SDK_VERSION, target_is_directory=True)
    lib_alias = sdk / "Lib" / CHROMIUM_SDK_VERSION
    if not lib_alias.exists():
        lib_alias.symlink_to(SDK_VERSION, target_is_directory=True)
    for alias, target in (("NCrypt.h", "ncrypt.h"), ("Winsock2.h", "winsock2.h")):
        header_alias = sdk / "Include" / SDK_VERSION / "um" / alias
        if not header_alias.exists():
            header_alias.symlink_to(target)

    toolchain_json = chromium_src / "build" / "win_toolchain.json"
    toolchain = {
        "path": str(root),
        "version": "2026",
        "win_sdk": str(sdk),
        "wdk": str(sdk),
        "runtime_dirs": [],
    }
    content = json.dumps(toolchain, indent=2) + "\n"
    if not toolchain_json.exists() or toolchain_json.read_text() != content:
        toolchain_json.write_text(content)

    print(f"Prepared Chromium xwin metadata in {root} and {toolchain_json}")


if __name__ == "__main__":
    main()
