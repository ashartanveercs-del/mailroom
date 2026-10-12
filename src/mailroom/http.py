"""The single I/O seam: every network call in mailroom is a `Request` handed to a `Transport`.

Adapters (Gmail, Drive, the LLM classifier) build requests; they never open sockets. That keeps
three things possible: a guard can inspect every request before it leaves (see `guard.py`), the
test suite can swap the network for in-process simulators, and retries live in one place.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class TransportError(Exception):
    """The request may or may not have reached the server (connection reset, timeout).

    Callers must treat this as "outcome unknown", never as "nothing happened"."""


class HttpError(Exception):
    """The server answered with an error status after retries. `reason` is the API's own
    code for the error when it gives one (a 403 can be a rate limit or a full drive)."""

    def __init__(self, status: int, message: str, *, reason: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.reason = reason


@dataclass(frozen=True)
class Request:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else {}

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class Transport(Protocol):
    def send(self, request: Request) -> Response: ...


class UrllibTransport:
    """The real network. Returns error statuses as responses; raises TransportError only when
    the outcome is unknown. Redirects are not followed: a 3xx comes back as a response."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def send(self, request: Request) -> Response:
        req = urllib.request.Request(request.url, data=request.body, method=request.method,
                                     headers=dict(request.headers))
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, dict(resp.headers.items()), resp.read())
        except urllib.error.HTTPError as err:
            headers = dict(err.headers.items()) if err.headers else {}
            return Response(err.code, headers, err.read() or b"")
        except (OSError, urllib.error.URLError) as err:
            raise TransportError(str(err)) from err


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect is a URL chosen by the server. Following it would bypass the allowlist.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class RetryingTransport:
    """Bounded exponential backoff on 429/5xx. Network errors are retried only for requests the
    caller marks idempotent (GETs always are; a Drive create with a pre-generated id is too,
    because a second create of the same id fails with 409 instead of making a copy)."""

    def __init__(self, inner: Transport, *, attempts: int = 4, base_delay: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 idempotent: Callable[[Request], bool] | None = None) -> None:
        self.inner = inner
        self.attempts = attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self.idempotent = idempotent or (lambda req: req.method == "GET")

    def send(self, request: Request) -> Response:
        delay = self.base_delay
        for attempt in range(1, self.attempts + 1):
            last = attempt == self.attempts
            try:
                response = self.inner.send(request)
            except TransportError:
                if last or not self.idempotent(request):
                    raise
                self.sleep(delay)
                delay *= 2
                continue
            if response.status in RETRY_STATUSES and not last:
                self.sleep(_retry_after(response, delay))
                delay *= 2
                continue
            return response
        raise AssertionError("unreachable")  # pragma: no cover


def _retry_after(response: Response, default: float) -> float:
    value = response.header("Retry-After")
    try:
        return min(float(value), 60.0) if value else default
    except ValueError:
        return default
