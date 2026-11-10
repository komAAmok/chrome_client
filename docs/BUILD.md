# Build layout

Chromium is built outside this repository. This repository stores only the
audited Core ABI header, platform binaries, manifests, and language bindings.

For local Core selection:

```sh
MINICRONET_CORE_DIR=/path/to/core/binaries/linux-x86_64 \
MINICRONET_REQUIRE_NATIVE=1 \
cargo check -p minicronet
```

The nine platform directories are intentionally explicit; no script silently
switches architecture.

After migrating a Core build, verify all targets and manifests:

```sh
tools/audit-core-binaries.sh
```

The ABI header, Rust declarations, Core definitions, and all three platform
export tables can be checked without a Chromium checkout:

```sh
tools/audit-abi.sh
```

Rust checks are independent of the native Core, while real Linux x86_64
smoke tests require the staged Core and its runtime loader path:

```sh
cargo check --workspace --all-targets
LD_LIBRARY_PATH="$PWD/core/binaries/linux-x86_64" \
  MINICRONET_REQUIRE_NATIVE=1 cargo test --workspace --all-targets
```

## Building the Core

All nine platform scripts need a pinned Chromium checkout; `tools/core-paths.sh`
resolves it from `CHROMIUM_SRC`, a sibling `../chromium/src`, or
`$HOME/chromium/src`.

```sh
tools/build-core-linux-x86_64.sh
tools/build-core-macos-arm64.sh
tools/build-core-windows-x86_64.sh
```

`tools/build-core-linux-armv7.sh` is the one script whose sysroot is not
fetched by `gclient runhooks`: `debian_bullseye_armhf-sysroot` is only
downloaded for `--arch=arm`. The script names the command when it is missing:

```sh
(cd "$CHROMIUM_SRC" && python3 build/linux/sysroot_scripts/install-sysroot.py --arch=arm)
MINICRONET_PKGCONF_DIR=/tmp/pkgconf/root tools/build-core-linux-armv7.sh
```

It sets `arm_float_abi = "hard"` explicitly. `build/config/arm.gni` defaults to
`softfp` whenever `target_cpu` differs from `v8_target_cpu`, which is this
build, and the armhf sysroot is hard-float; leaving the default would mix two
ABIs in one link.

Each script syncs the Core sources, builds, strips, audits, and leaves the
artifact in `$CHROMIUM_SRC/out/MiniCronet-<target>/`. Installing into the
repository is a separate step:

```sh
python3 tools/install-core-binaries.py --abi-version 8 --chromium-src <checkout>
tools/audit-core-binaries.sh
```

A version bump requires a rebuild of every platform: `audit-core-binaries.sh`
reads the `minicronet/<version>` marker out of each artifact's bytes and fails
when it disagrees with the workspace version.

### The committed args.gn cannot be regenerated: read this before rebuilding

Everything above assumes `gn gen` runs. **It does not, with the configuration in
this repository.** The committed `args.gn` (written by `tools/sync-core.sh` and the
build scripts) turns off `use_aura`, `use_ozone`, `use_gtk`, `use_gio`, `use_glib`,
`use_dbus` and `use_udev`, which the Chromium `//ui` layer does not tolerate:

```text
$ ./buildtools/linux64/gn gen out/MiniCronet-linux-x86_64
ERROR at //ui/gtk/BUILD.gn:16  assert(use_gio, "GIO is required for building with GTK")
```

Patching one assertion only reveals the next; four have been traced
(`docs/LESSONS.md` #48 lists all of them). The root cause is structural: `//ui`
assumes every desktop platform has *some* window-system implementation, and this
build deliberately has none. Two remediation paths (fix the args, empty out
`gn_all`) were tried and are recorded as rejected in stage 0 of
`docs/NEXT_STEPS.md`.

**What this means in practice:**

- An existing `out/MiniCronet-<target>/` can still build **incrementally** with
  `ninja`, because its `build.ninja` was generated at some earlier point when the
  configuration was self-consistent. That is how every artifact in
  `core/binaries/` was produced.
- A **clean** rebuild (`rm -rf out/...` then `gn gen`) is not possible from the
  repository as it stands.
- Consequently a manifest `sha256` means *"this exact artifact was built and then
  audited"*, **not** *"this artifact can be reproduced from this commit"*.
- `core/build-args/<target>.snapshot.txt` records what each existing artifact was
  actually built with, recovered from its generated buildflag headers (evidence,
  not intent). `python3 tools/record-core-build-args.py --check` fails when a
  rebuild changes that configuration without re-recording it.

If you must rebuild, the incremental path is:

```sh
tools/sync-core.sh                                  # apply the 15 patches
ninja -C "$CHROMIUM_SRC/out/MiniCronet-linux-x86_64" libminicronet.so
$CHROMIUM_SRC/third_party/llvm-build/Release+Asserts/bin/llvm-objcopy \
  --strip-unneeded "$CHROMIUM_SRC/out/MiniCronet-linux-x86_64/libminicronet.so" \
  "$CHROMIUM_SRC/out/MiniCronet-linux-x86_64/libminicronet.stripped.so"
python3 tools/record-core-build-args.py            # re-record the config
python3 tools/install-core-binaries.py --abi-version 8 --chromium-src "$CHROMIUM_SRC"
python3 tools/audit-core-payloads.py               # payload bytes unchanged
tools/audit-core-binaries.sh                       # nine targets, sha256 + markers
```

`install-core-binaries.py` rewrites each manifest's `sha256` and `size_bytes`;
`audit-core-payloads.py` is the only cross-platform proof that behaviour did not
change, so it is not optional. See stage 0 in `docs/NEXT_STEPS.md` for the full
rationale and for what the static gates can and cannot catch.

### Linux cross-builds need pkg-config

`gn` invokes `pkg-config` for NSS (`build/config/linux/nss/BUILD.gn`), and the
Linux cross targets have no system one. The package is deliberately **not**
vendored here; fetch and unpack it outside the repository:

```sh
mkdir -p /tmp/pkgconf && cd /tmp/pkgconf
apt-get download pkgconf pkgconf-bin libpkgconf3   # or copy the three .debs
mkdir -p root && for deb in *.deb; do dpkg-deb -x "$deb" root/; done
```

Then point `MINICRONET_PKGCONF_DIR` at the unpacked root:

```sh
MINICRONET_PKGCONF_DIR=/tmp/pkgconf/root tools/build-core-linux-x86.sh
MINICRONET_PKGCONF_DIR=/tmp/pkgconf/root tools/build-core-linux-arm64.sh
```

The scripts put `$MINICRONET_PKGCONF_DIR/usr/bin` on `PATH` and
`$MINICRONET_PKGCONF_DIR/usr/lib/x86_64-linux-gnu` on `LD_LIBRARY_PATH`, which
is what `libpkgconf.so.3` needs; running the unpacked `pkg-config` directly
without that fails with "cannot open shared object file".

Do **not** set `PKG_CONFIG_SYSROOT_DIR`. Chromium's `build/config/linux/pkg-config.py`
already passes `--sysroot` and computes `PKG_CONFIG_LIBDIR` from it, so a second
prefix yields `.../sysroot/<checkout>/build/linux/debian_bullseye_i386-sysroot/usr/include/nss`
and the build dies on `pk11pub.h: file not found`.

`PKG_CONFIG_LIBDIR` does not need to be set either: the wrapper overwrites it
from the sysroot it is given. Verified by building `linux-x86` with
`PKG_CONFIG_LIBDIR=/nonexistent/bogus/path` and getting a byte-identical
artifact (`069276f884924c4f`).
