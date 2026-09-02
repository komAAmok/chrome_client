#!/usr/bin/env python3
"""Expose an osxcross SDK as Chromium's hermetic Xcode layout."""

import plistlib
import subprocess
import sys
from pathlib import Path


SDK_VERSION = "26.5"
SDK_BUILD = "25F70"
XCODE_VERSION = "26.5"
XCODE_BUILD = "17F42"


def fail(message: str) -> None:
    raise SystemExit(message)


def replace_symlink(path: Path, target: Path) -> None:
    if path.is_symlink():
        if path.resolve() == target.resolve():
            return
        path.unlink()
    elif path.exists():
        fail(f"refusing to replace non-symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=True)


def main() -> None:
    if len(sys.argv) != 3:
        fail(f"usage: {Path(sys.argv[0]).name} OSXCROSS_TARGET CHROMIUM_SRC")

    target = Path(sys.argv[1]).resolve()
    chromium = Path(sys.argv[2]).resolve()
    sdk = target / "SDK" / f"MacOSX{SDK_VERSION}.sdk"
    clang_runtime = (
        chromium
        / "third_party"
        / "llvm-build"
        / "Release+Asserts"
        / "lib"
        / "clang"
        / "23"
        / "lib"
        / "darwin"
        / "libclang_rt.osx.a"
    )
    readobj = (
        chromium
        / "third_party"
        / "llvm-build"
        / "Release+Asserts"
        / "bin"
        / "llvm-readobj"
    )
    required = (
        target / "bin" / "osxcross-conf",
        target / "bin" / "x86_64-apple-darwin25.5-clang",
        target / "bin" / "arm64-apple-darwin25.5-clang",
        sdk / "SDKSettings.plist",
        sdk / "usr" / "lib" / "libSystem.tbd",
        chromium / "third_party" / "llvm-build" / "Release+Asserts" / "bin" / "ld64.lld",
        readobj,
        clang_runtime,
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        fail("osxcross toolchain is incomplete:\n" + "\n".join(missing))

    with (sdk / "SDKSettings.plist").open("rb") as stream:
        sdk_settings = plistlib.load(stream)
    system_version_path = sdk / "System" / "Library" / "CoreServices" / "SystemVersion.plist"
    if not system_version_path.exists():
        fail(f"missing SDK system version metadata: {system_version_path}")
    with system_version_path.open("rb") as stream:
        system_version = plistlib.load(stream)
    if (
        sdk_settings.get("Version") != SDK_VERSION
        or system_version.get("ProductBuildVersion") != SDK_BUILD
    ):
        fail(f"expected macOS SDK {SDK_VERSION} ({SDK_BUILD})")

    result = subprocess.run(
        (readobj, "--file-headers", clang_runtime),
        check=False,
        capture_output=True,
        text=True,
    )
    arches = {
        line.removeprefix("Arch: ")
        for line in result.stdout.splitlines()
        if line.startswith("Arch: ")
    }
    if result.returncode or not {"x86_64", "aarch64"}.issubset(arches):
        fail("libclang_rt.osx.a must contain x86_64 and arm64 slices")

    xcode = chromium / "build" / "mac_files" / "xcode_binaries"
    contents = xcode / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    version = {
        "CFBundleShortVersionString": XCODE_VERSION,
        "ProductBuildVersion": XCODE_BUILD,
    }
    version_path = contents / "version.plist"
    encoded = plistlib.dumps(version, fmt=plistlib.FMT_XML, sort_keys=True)
    if not version_path.exists() or version_path.read_bytes() != encoded:
        version_path.write_bytes(encoded)

    developer = contents / "Developer"
    replace_symlink(
        developer / "Platforms" / "MacOSX.platform" / "Developer" / "SDKs" / sdk.name,
        sdk,
    )
    replace_symlink(
        developer / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin",
        target / "bin",
    )

    print(f"Prepared Chromium macOS SDK {SDK_VERSION} from {target}")


if __name__ == "__main__":
    main()
