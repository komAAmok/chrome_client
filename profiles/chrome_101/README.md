# chrome_101

Reference captures for Chromium 101.0.4951.67. Six independent
connections per protocol, captured against tls.peet.ws /api/all (h1, h2)
and quic.tools.scrapfly.io/api/fp/quic (h3).

## Layout

| Path | Contents |
| --- | --- |
| `meta.json` | version, capture target and completeness per protocol |
| `h2/captures.json` | six h2 records, concatenated JSON objects |
| `h2/samples.json` | the same six samples with their `checks` metadata |
| `h1/captures.json`, `h1/samples.json` | the same, forced HTTP/1.1 |
| `h3/captures.json`, `h3/samples.json` | the same, forced HTTP/3 |

`captures.json` holds the endpoint's own response objects, unmodified.
`samples.json` adds what the response does not carry: ja3/ja4/peetprint
hashes, the session id, the client random and the header count, per
connection.

## Protocol status

| Protocol | Connections | Status |
| --- | --- | --- |
| h2 | 6 | complete |
| h1 | 6 | complete |
| h3 | 6 | complete |

## Reproduce the wire gate

```sh
tools/verify-wire-capture.py --profile chrome_101 \
  --captures profiles/chrome_101/h2/captures.json \
  --out profiles/chrome_101/h2/validation.json
```

## Provenance

Imported from the reference capture set by
`tools/import-reference-captures.py`. The set is not part of this
repository: it is what a real Chrome build produced on the machine that
ran the capture, and `meta.json` records the executable path and target
each entry was collected against.
