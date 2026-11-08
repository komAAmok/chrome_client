# Platform support

| Platform | Target |
| --- | --- |
| Windows | x86, x86_64, ARM64 |
| Linux | x86, x86_64, ARMv7 (hard-float), ARM64 |
| macOS | x86_64, ARM64 |

Android, Java, public JNI, UI, and unrelated Chromium targets are outside this
project.

## Linux ARMv7 is hard-float only

The ARMv7 artifact is built for `armv7-unknown-linux-gnueabihf`: EABI5, hard
float, `arm_float_abi = "hard"`. `tools/build-core-linux-armv7.sh` sets that
explicitly because `build/config/arm.gni` falls back to `softfp` whenever
`target_cpu` differs from `v8_target_cpu`, which is this build. A soft-float
userland cannot load it, and mixing the two in one process is undefined.

`build/config/arm.gni` also sets `arm_use_neon = true` with no per-target
override, so the artifact requires NEON. Every ARMv7 core Chromium supports for
its own builds has it; a pre-NEON core (ARMv7-A without the Advanced SIMD
extension) will raise SIGILL rather than report a missing feature.

## Runtime evidence per platform

| Target | Build | Loaded and issued a request |
| --- | --- | --- |
| linux-x86_64 | yes | yes (`tools/run-http-smoke.py` on the build host) |
| linux-x86 | yes | no emulator on the build host |
| linux-armv7 | yes | no emulator on the build host |
| linux-arm64 | yes | no emulator on the build host |
| windows-x86 / windows-x86_64 / windows-arm64 | yes | no (cross-build only) |
| macos-x86_64 / macos-arm64 | yes | no (cross-build only) |

Only `linux-x86_64` has a recorded run on this project, which is the same thing
`README.md` says. A ✓ in the table above means "committed and audited", not
"executed": the audit is static (symbols, sizes, loader name, embedded payloads)
and cannot tell a loadable artifact from an unloadable one.
