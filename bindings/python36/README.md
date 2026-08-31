# Python 3.6 native compatibility

This crate is intentionally maintained separately from the modern extension:

- Python 3.7–3.13: `bindings/python`, PyO3 0.28.3, `abi3-py37`.
- Python 3.6: `bindings/python36`, PyO3 0.15.1, `abi3-py36`.

Both extensions use the same Rust/Core layer (`crates/minicronet`). The public
Python import remains `chrome_client`; packaging selects the 3.6 native module
for Python 3.6 and must not mix the two extensions in one interpreter.

The shared requests facade supports both a single `proxy` URL and a Requests-style
`proxies` mapping keyed by scheme (`http`, `https`, or `all`).
