#!/usr/bin/env python3
"""Installs freshly built Cores into core/binaries and refreshes their manifests.

Copies each platform artifact out of the Chromium build directory, then rewrites
`manifest.json` with the new SHA-256, size and ABI version. Refuses to touch a
target whose build directory is missing so a partial rebuild cannot leave the
repository holding a mix of ABI versions.

    tools/install-core-binaries.py --abi-version 8
    tools/install-core-binaries.py --abi-version 8 --targets linux-x86_64
"""

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CHROMIUM = pathlib.Path("/home/sj/chromium/src")

# target -> (build dir suffix, primary library, extra files)
TARGETS = {
    "linux-x86": ("MiniCronet-linux-x86", "libminicronet.so", ()),
    "linux-x86_64": ("MiniCronet-linux-x86_64", "libminicronet.so", ()),
    "linux-arm64": ("MiniCronet-linux-arm64", "libminicronet.so", ()),
    # The MSVC link step emits minicronet.dll.lib; the repository stores it as
    # minicronet.lib, so the import library is renamed on the way in.
    "windows-x86": ("MiniCronet-win-x86", "minicronet.dll",
                    (("minicronet.dll.lib", "minicronet.lib"),)),
    "windows-x86_64": ("MiniCronet-win-x86_64", "minicronet.dll",
                       (("minicronet.dll.lib", "minicronet.lib"),)),
    "windows-arm64": ("MiniCronet-win-arm64", "minicronet.dll",
                      (("minicronet.dll.lib", "minicronet.lib"),)),
    "macos-x86_64": ("MiniCronet-macos-x86_64", "libminicronet.dylib", ()),
    "macos-arm64": ("MiniCronet-macos-arm64", "libminicronet.dylib", ()),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def elf_needed(path):
    """Reads DT_NEEDED so the manifest records what the loader actually wants."""
    try:
        output = subprocess.run(
            ["readelf", "-d", str(path)], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    needed = []
    for line in output.splitlines():
        if "(NEEDED)" in line and "[" in line:
            needed.append(line.split("[", 1)[1].split("]", 1)[0])
    return needed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--abi-version", type=int, required=True)
    parser.add_argument("--chromium-src", type=pathlib.Path, default=DEFAULT_CHROMIUM)
    parser.add_argument("--targets", nargs="*", default=sorted(TARGETS))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    revision = (ROOT / "CHROMIUM_REVISION").read_text().strip()
    unknown = [name for name in args.targets if name not in TARGETS]
    if unknown:
        parser.error("unknown targets: %s" % ", ".join(unknown))

    missing = []
    for name in args.targets:
        out_dir, library, _ = TARGETS[name]
        if not (args.chromium_src / "out" / out_dir / library).is_file():
            missing.append("%s (%s)" % (name, out_dir))
    if missing:
        print("refusing to install a partial set; not built: %s" % ", ".join(missing),
              file=sys.stderr)
        return 1

    for name in args.targets:
        out_dir, library, extras = TARGETS[name]
        source_dir = args.chromium_src / "out" / out_dir
        dest_dir = ROOT / "core/binaries" / name
        manifest_path = dest_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text())

        for source_name, dest_name in ((library, library),) + extras:
            source = source_dir / source_name
            if not source.is_file():
                print("%s: missing %s" % (name, source_name), file=sys.stderr)
                return 1
            if not args.dry_run:
                shutil.copy2(source, dest_dir / dest_name)

        # Hash the source under --dry-run: the destination still holds the old
        # artifact, and reporting that would look like a no-op rebuild.
        installed = source_dir / library if args.dry_run else dest_dir / library
        manifest["abi_version"] = args.abi_version
        manifest["chromium_revision"] = revision
        manifest["sha256"] = sha256(installed)
        manifest["size_bytes"] = installed.stat().st_size
        for source_name, dest_name in extras:
            if dest_name.endswith(".lib"):
                manifest["import_library_sha256"] = sha256(
                    source_dir / source_name if args.dry_run else dest_dir / dest_name)
        needed = elf_needed(installed) if library.endswith(".so") else None
        if needed:
            loader = [entry for entry in needed if entry.startswith("ld-")]
            manifest["runtime_dependencies"] = sorted(
                set(needed) - set(loader)) + sorted(loader)

        if not args.dry_run:
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print("%-16s %s  %d bytes  abi=%d" % (
            name, manifest["sha256"][:16], manifest["size_bytes"], args.abi_version))

    targets_path = ROOT / "core/binaries/targets.json"
    targets = json.loads(targets_path.read_text())
    targets["abi_version"] = args.abi_version
    if not args.dry_run:
        targets_path.write_text(json.dumps(targets, indent=2) + "\n")
    print("targets.json abi_version=%d" % args.abi_version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
