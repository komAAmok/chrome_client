# MiniCronet ABI

`minicronet.h` is the only public native header. It is copied from the audited
Core build output during release preparation and must remain synchronized with
`ABI_VERSION` and the exported symbol list.

`ABI_VERSION` is a bare integer that must equal the header's `MN_ABI_VERSION`.
`tools/audit-abi.sh` compares the two (and the Rust FFI's copy of the number) on
every run: the file sat at 8 for two releases after the header moved to 10,
because nothing read it. Edit it in the same commit as the header.

Do not add Chromium or Cronet public headers here.
