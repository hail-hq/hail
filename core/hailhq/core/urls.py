"""URL canonicalization across language boundaries.

Trailing slashes are the most common URL-handling foot-gun in the Hail
stack. Pydantic ``AnyHttpUrl`` (this side — used by FastMCP and FastAPI)
adds a trailing slash to root URLs; the WHATWG URL spec (Node, browser TS)
does not. A URL minted on one side and compared on the other fails
exact-string equality — Better Auth's oauth-provider does ``Set.has()``
against ``validAudiences``, PyJWT does ``==`` on ``iss`` and ``aud``.
Wire-level tools like curl don't normalize either.

Convention everywhere in this codebase:

* **Storage / config / construction** → :func:`canonical_url` (no trailing slash).
* **Matching against config** → :func:`url_variants` (both forms) so we tolerate
  whatever the other end produced.
* **Building child URLs** → :func:`join_url` — never f-string ``{base}/{path}``
  raw, which double-slashes when ``base`` ends in ``/``.

If a URL crosses a language/library boundary and lands in any kind of
comparison, reach for these helpers. URLs are not strings.
"""

from __future__ import annotations


def canonical_url(url: str) -> str:
    """Strip every trailing ``/`` from a URL. The canonical form for storage."""
    return url.rstrip("/")


def url_variants(url: str) -> list[str]:
    """Both forms — with and without a single trailing slash — for tolerant
    exact-string matching against URLs minted elsewhere.

    The Python side typically stores the no-slash canonical form, but a
    JS-rooted partner (e.g. tokens minted by Better Auth's oauth-provider
    after Claude sent ``resource=https://mcp.hail.so/``) may send the slash
    form. Comparing against this list lets either pass.
    """
    canonical = canonical_url(url)
    return [canonical, f"{canonical}/"]


def join_url(base: str, path: str) -> str:
    """Slash-aware URL join.

    Strips trailing slashes from ``base`` and leading slashes from ``path``,
    then joins with a single ``/``. Use this when constructing child URLs
    (e.g. JWKS URL = ``join_url(auth_url, "jwks")``) so the result never
    accidentally double-slashes.
    """
    return f"{canonical_url(base)}/{path.lstrip('/')}"


__all__ = ["canonical_url", "join_url", "url_variants"]
