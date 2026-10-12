"""The request allowlist: nothing leaves the process unless a rule allows exactly that request.

A rule names one API operation: HTTP method, exact host, a regular expression that must match
the whole path, the query keys it may carry (with optional exact values and value patterns),
and an optional body check. `GuardedTransport` runs the check *before* handing the request to
the real transport, so an adapter bug (or a hostile value smuggled into a URL) is stopped
before it reaches the wire rather than discovered in an audit later.

What the guard refuses even when a rule's path matches:
  * anything but https on the default port, credentials in the URL, fragments
  * `..`, `//` and backslashes in the path (no path games behind a matching prefix)
  * unknown or repeated query keys (repeats invite parser differentials)
  * method-override headers, which many HTTP APIs honour to run a POST as PATCH or DELETE
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .http import Request, Response, Transport

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
OVERRIDE_HEADERS = frozenset({"x-http-method-override", "x-http-method", "x-method-override"})


class BlockedRequest(Exception):
    """The request is not on the allowlist. Raised before anything is sent."""


class UnsafePageToken(BlockedRequest):
    """A next-page token that is not an opaque token, repeats, or runs past the page cap."""


@dataclass(frozen=True)
class Rule:
    name: str
    method: str
    host: str
    path: str
    query: frozenset[str] | set[str] = frozenset()
    required: Mapping[str, str] = field(default_factory=dict)
    validators: Mapping[str, str] = field(default_factory=dict)
    body: Callable[[Request], None] | None = None

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"rule {self.name}: unknown method {self.method!r}")
        object.__setattr__(self, "host", self.host.lower())
        object.__setattr__(self, "query", frozenset(self.query))
        object.__setattr__(self, "_path_re", re.compile(self.path))
        object.__setattr__(self, "_validators",
                           {k: re.compile(v) for k, v in self.validators.items()})

    def matches_route(self, method: str, host: str, path: str) -> bool:
        return (method == self.method and host == self.host
                and self._path_re.fullmatch(path) is not None)  # type: ignore[attr-defined]

    def check_query(self, pairs: list[tuple[str, str]]) -> None:
        seen: set[str] = set()
        for key, value in pairs:
            if key in seen:
                raise BlockedRequest(f"{self.name}: query parameter {key!r} is repeated")
            seen.add(key)
            if key not in self.query and key not in self.required:
                raise BlockedRequest(f"{self.name}: unexpected query parameter {key!r}")
            pattern = self._validators.get(key)  # type: ignore[attr-defined]
            if pattern is not None and not pattern.fullmatch(value):
                raise BlockedRequest(f"{self.name}: value of {key!r} is not allowed")
        values = dict(pairs)
        for key, expected in self.required.items():
            if values.get(key) != expected:
                raise BlockedRequest(f"{self.name}: {key} must be {expected!r}")

    def describe(self) -> dict[str, object]:
        query = sorted(self.query) + [f"{k}={v}" for k, v in sorted(self.required.items())]
        return {"name": self.name, "method": self.method, "host": self.host,
                "path": self.path, "query": query, "body_checked": self.body is not None}


class Allowlist:
    def __init__(self, rules: Iterable[Rule]) -> None:
        self.rules = list(rules)

    def __add__(self, other: Allowlist) -> Allowlist:
        return Allowlist(self.rules + other.rules)

    def check(self, request: Request) -> Rule:
        """The rule that allows this request; BlockedRequest for anything else."""
        method = request.method
        for name in request.headers:
            if name.lower() in OVERRIDE_HEADERS:
                raise BlockedRequest(f"method override header {name!r} is not allowed")
        try:
            parts = urllib.parse.urlsplit(request.url)
            port = parts.port
        except ValueError as err:
            raise BlockedRequest(f"malformed URL: {err}") from None
        if parts.scheme != "https":
            raise BlockedRequest("only https requests are allowed")
        if parts.username is not None or parts.password is not None:
            raise BlockedRequest("credentials in the URL are not allowed")
        if port not in (None, 443):
            raise BlockedRequest(f"port {port} is not allowed")
        if parts.fragment or request.url.endswith("#"):
            raise BlockedRequest("a URL fragment is not allowed")
        host = (parts.hostname or "").lower()
        if not any(rule.host == host for rule in self.rules):
            raise BlockedRequest(f"host {host!r} is not on the allowlist")
        path = parts.path
        if "//" in path or "\\" in path or any(seg in (".", "..") for seg in path.split("/")):
            raise BlockedRequest(f"path {path!r} is not allowed")
        try:
            pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True,
                                           strict_parsing=bool(parts.query))
        except ValueError:
            raise BlockedRequest("malformed query string") from None
        candidates = [rule for rule in self.rules if rule.matches_route(method, host, path)]
        if not candidates:
            raise BlockedRequest(f"{method} {host}{path} is not on the allowlist")
        errors = []
        for rule in candidates:
            try:
                rule.check_query(pairs)
                if rule.body is not None:
                    rule.body(request)
            except BlockedRequest as err:
                errors.append(err)
                continue
            return rule
        raise errors[0]

    def describe(self) -> list[dict[str, object]]:
        return [rule.describe() for rule in self.rules]


class GuardedTransport:
    """Checks every request against the allowlist, then forwards it. `audit` keeps one
    (rule name, method, path) entry per request that was let through."""

    def __init__(self, inner: Transport, allowlist: Allowlist) -> None:
        self.inner = inner
        self.allowlist = allowlist
        self.audit: list[tuple[str, str, str]] = []

    def send(self, request: Request) -> Response:
        rule = self.allowlist.check(request)
        self.audit.append((rule.name, request.method, urllib.parse.urlsplit(request.url).path))
        return self.inner.send(request)


# Opaque page tokens from Google APIs are digits or URL-safe base64 with a few markers
# (`~!!~...`). Anything with ':', '&', whitespace or control characters is not a token.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.~!=+/]{1,2048}")


class PageCursor:
    """Validates the next-page token of each page before it goes into the next request.

    The token is re-checked here and then encoded into the adapter's *own* URL (which the
    guard checks again). A URL handed back by the server is never requested."""

    def __init__(self, max_pages: int = 1000) -> None:
        self.max_pages = max_pages
        self.pages = 0
        self._seen: set[str] = set()

    def advance(self, token: object) -> str | None:
        self.pages += 1
        if token is None or token == "":
            return None
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise UnsafePageToken("the next-page token is not an opaque token")
        if token in self._seen:
            raise UnsafePageToken("the next-page token repeated an earlier page")
        if self.pages >= self.max_pages:
            raise UnsafePageToken(f"the listing ran to more than {self.max_pages} pages")
        self._seen.add(token)
        return token
