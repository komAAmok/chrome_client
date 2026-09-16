"""Type stub for ``chrome_client.api``.

At runtime this module is an alias of ``chrome_client._python_impl.api``
installed into ``sys.modules`` on import; these explicit same-name re-exports
expose the alias to type checkers (PEP 561).
"""

from ._python_impl.api import (
    async_session as async_session,
    close_shared_session as close_shared_session,
    delete as delete,
    get as get,
    head as head,
    options as options,
    patch as patch,
    post as post,
    put as put,
    query as query,
    request as request,
    session as session,
    shared_session as shared_session,
    trace as trace,
)
