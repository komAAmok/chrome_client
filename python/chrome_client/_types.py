"""
Type definitions and constants for chrome_client.
"""

from typing import Union, Mapping, Sequence, Tuple, Any, Optional


# Type aliases
HeadersType = Union[
    Mapping[str, Optional[str]], Sequence[Tuple[str, Optional[str]]]
]
CookiesType = Union[Mapping[str, str], 'CookieJar']
DataType = Union[
    str, bytes, bytearray, Mapping[str, Any], Sequence[Tuple[str, Any]], None
]
