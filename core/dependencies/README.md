# Runtime dependencies

Linux Core binaries dynamically require architecture-matched system NSS/NSPR:
`libnss3.so`, `libnssutil3.so`, `libnspr4.so`, and NSS's `libplc4.so`/
`libplds4.so` dependencies. They also require the matching platform glibc,
libdl, libpthread, libm, libgcc_s, and ELF loader. Install these from the
target distribution (for example `libnss3` and `libnspr4`); do not copy an
Ubuntu x86_64 library into x86 or ARM64 packages and do not statically link
glibc/NSS.

Windows ICU data is staged under the matching target directory. Platform-
specific bundled dependencies belong under the matching target directory.
Do not place Chromium source, Android files, Java/JNI artifacts, or developer
build products here.
