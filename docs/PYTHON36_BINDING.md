# Python 3.6 native compatibility

This crate is intentionally maintained separately from the modern extension:

- Python 3.9–3.15: `bindings/python`, PyO3 0.30.1, `abi3-py39`, `cp39-abi3`.
- Python 3.6–3.8: `bindings/python36`, PyO3 0.15.1, `abi3-py36`, `cp36-abi3`.

The dividing line is 3.9 because that is pyo3 0.30.1's own minimum:
`MINIMUM_SUPPORTED_VERSION = { major: 3, minor: 9 }` in
`pyo3-build-config-0.30.1/src/impl_.rs:26`, and 0.30.1's feature table has
`abi3-py39` as its lowest abi3 feature (`abi3-py37`/`abi3-py38` do not
exist there), so a `cp37`/`cp38` wheel cannot be built from that crate. The
first clause of that same file (`impl_.rs:82-89`) rejects the other direction:
building `abi3-py39` with a 3.7 interpreter fails with "cannot set a minimum
Python version 3.9 higher than the interpreter version 3.7". 3.6–3.8 stay on
0.15.1, whose `ABI3_MAX_MINOR` is 9 and whose `abi3-py36` covers them.

Both extensions use the same Rust/Core layer (`crates/minicronet`). The public
Python import remains `chrome_client`; packaging selects the 3.6 native module
for Python 3.6 and must not mix the two extensions in one interpreter.

The shared requests facade supports both a single `proxy` URL and a Requests-style
`proxies` mapping keyed by scheme (`http`, `https`, or `all`).
