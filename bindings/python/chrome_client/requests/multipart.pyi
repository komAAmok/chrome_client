"""Form, JSON and multipart body encoding, plus ``CurlMime``.

At runtime this module *is* ``chrome_client._python_impl.multipart``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.multipart import (
    EncodedMultipart as EncodedMultipart,
    BodySource as BodySource,
    Part as Part,
    CurlMime as CurlMime,
    encode_params as encode_params,
    encode_multipart as encode_multipart,
    json_body as json_body,
    iter_body as iter_body,
    is_stream_body as is_stream_body,
    body_length as body_length,
)
