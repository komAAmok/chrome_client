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

### Regenerating the build files: use `--root-target`, and install pkgconf first

`gn gen` works. If it fails, the cause is almost never the `args.gn` — it is a
missing host tool or the wrong command line.

**Run it the way ninja runs it.** A bare `gn gen out/...` parses the whole tree,
including the `gn_all` test targets, and those assert on window-system flags this
build deliberately turns off (`//ui/gtk:16 assert(use_gio)`). The regeneration
command recorded in `build.ninja` scopes it to the Core:

```bash
cd "$CHROMIUM_SRC/out/MiniCronet-<target>"
../../buildtools/linux64/gn --root=../.. -q --fail-on-unused-args --regeneration \
  --root-pattern="//minicronet:*" --root-target=//minicronet gen .
```

**Linux and macOS targets need pkg-config.** `//net` reaches it through
`//build/config/linux/nss/BUILD.gn:11`. Without it, gn stops with
`FileNotFoundError: ... 'pkg-config'`. The unpack recipe is in the next section.

**Windows targets need ripgrep and xwin.** `tools/build-core-windows-*.sh` runs an
Android/Java/JNI leak check with `rg`, and resolves the MSVC headers from
`$XWIN_ROOT`.

This was misdiagnosed once as an irreconcilable conflict between
`is_minicronet_build` and the `//ui` layer; `docs/LESSONS.md` #48 records the
whole thing, including why the four-assertion chain was an artefact of running the
wrong command.

**A manifest `sha256` is a reproducibility claim**, given the toolchain above:
`0.2.10` was rebuilt from scratch on all nine platforms from this commit.
`core/build-args/<target>.snapshot.txt` and
`python3 tools/record-core-build-args.py --check` guard against a rebuild that
silently changes the configuration.

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
