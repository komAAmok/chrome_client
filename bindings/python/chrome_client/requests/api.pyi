"""Module-level ``get``/``post``/... over one process-wide session.

At runtime this module *is* ``chrome_client._python_impl.api``: the package aliases it
into ``sys.modules`` on import. The names below are re-exported
explicitly, so a type checker follows the same path instead of
seeing ``Any`` (PEP 561).
"""

from .._python_impl.api import (
    shared_session as shared_session,
    close_shared_session as close_shared_session,
    session as session,
    async_session as async_session,
    request as request,
    get as get,
    options as options,
    head as head,
    post as post,
    put as put,
    patch as patch,
    delete as delete,
    trace as trace,
    query as query,
)
