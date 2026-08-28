# Core export definitions

These three files are the platform-specific spelling of the same ABI v7
symbol set. They must remain identical in membership and order:

- `minicronet.def` for Windows;
- `minicronet.exports` for macOS;
- `minicronet.lds` for ELF/Linux.

The public declarations remain solely in `core/abi/minicronet.h`. Any symbol
added here must first be added to that header, `minicronet-sys`, and the ABI
audit.
