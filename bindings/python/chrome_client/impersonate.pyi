"""Profile selection: ``Literal`` lists of every accepted value.

At runtime this module *is* ``chrome_client._python_impl.impersonate``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from ._python_impl.impersonate import (
    LATEST_CHROME as LATEST_CHROME,
    OLDEST_CHROME as OLDEST_CHROME,
    ALIASES as ALIASES,
    ChromeProfileName as ChromeProfileName,
    ChromeProfileAlias as ChromeProfileAlias,
    ChromeFamilyAlias as ChromeFamilyAlias,
    Impersonate as Impersonate,
    HttpVersion as HttpVersion,
    CurlHttpVersion as CurlHttpVersion,
    HTTP_VERSIONS as HTTP_VERSIONS,
    normalize_http_version as normalize_http_version,
    normalize_impersonate as normalize_impersonate,
    validate_extra_fp as validate_extra_fp,
    reject_fingerprint_overrides as reject_fingerprint_overrides,
    available_profiles as available_profiles,
    ExtraFingerprints as ExtraFingerprints,
)
