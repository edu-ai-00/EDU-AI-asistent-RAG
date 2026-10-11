"""HTTP for calls that carry a secret (API key, bearer token): a redirect is refused, never followed.

urllib follows a 3xx on its own and copies the request headers onto the new request, so an
`x-api-key` or `Authorization` header sent to Langflow would be forwarded to whatever host
the Location header names. Here a 3xx surfaces as an HTTPError carrying the status code and
nothing is sent anywhere else. The tooling talks to a fixed local Langflow URL, so a
redirect is never part of normal operation.

Standard library only. Shared by create_api_key.py, bootstrap.py, import_flows.py and smoke.py.
"""

from __future__ import annotations

import urllib.request


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Answer every redirect with "no new request": urllib then raises HTTPError for the 3xx."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - urllib's hook
        return None


def make_opener(*extra_handlers) -> urllib.request.OpenerDirector:
    """The standard opener with redirects refused. `extra_handlers` exist for tests."""
    return urllib.request.build_opener(RefuseRedirect, *extra_handlers)


OPENER = make_opener()


def open_url(request, data=None, **kwargs):
    """Like urllib.request.urlopen (same arguments), but a redirect is an HTTPError, not a second request."""
    return OPENER.open(request, data, **kwargs)
