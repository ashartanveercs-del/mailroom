"""OAuth for Google APIs: a refresh token in, short-lived access tokens in memory.

The credential file (Google's "authorized user" JSON) is only ever read. Access tokens live in
memory for the life of the process and are never written or logged. The token request goes
through the same guarded transport as every API call, where the allowlist only admits a
refresh-token grant.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..http import Request, Response, Transport
from .policy import TOKEN_URI

EXPIRY_SKEW = 60.0  # refresh this many seconds before the token expires


class AuthError(Exception):
    """The Google connection needs to be set up again (no usable refresh token)."""


class OAuthCredentials:
    def __init__(self, *, client_id: str, client_secret: str, refresh_token: str,
                 transport: Transport, clock: Callable[[], float] = time.time) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._transport = transport
        self._clock = clock
        self._access: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return f"OAuthCredentials(client_id={self._client_id!r}, secrets=<hidden>)"

    @classmethod
    def from_file(cls, path: str | Path, *, transport: Transport,
                  clock: Callable[[], float] = time.time) -> OAuthCredentials:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise AuthError(f"credential file not found: {path}") from None
        except (OSError, ValueError):
            raise AuthError(f"credential file is not readable JSON: {path}") from None
        if not isinstance(data, dict):
            raise AuthError("credential file must hold a JSON object")
        missing = [k for k in ("client_id", "client_secret", "refresh_token")
                   if not isinstance(data.get(k), str) or not data.get(k)]
        if missing:
            raise AuthError(f"credential file is missing {', '.join(missing)}")
        return cls(client_id=data["client_id"], client_secret=data["client_secret"],
                   refresh_token=data["refresh_token"], transport=transport, clock=clock)

    def token(self) -> str:
        if self._access is None or self._clock() >= self._expires_at - EXPIRY_SKEW:
            self.refresh()
        assert self._access is not None
        return self._access

    def refresh(self) -> None:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token", "client_id": self._client_id,
            "client_secret": self._client_secret, "refresh_token": self._refresh_token,
        }).encode("ascii")
        response = self._transport.send(Request(
            "POST", TOKEN_URI, headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body))
        if response.status != 200:
            raise AuthError(f"Google refused the refresh token (HTTP {response.status}); "
                            "reconnect the account")
        try:
            data = response.json()
            self._access = str(data["access_token"])
            self._expires_at = self._clock() + float(data.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as err:
            raise AuthError("Google sent an unexpected token response") from err


class AuthorizedTransport:
    """Adds the bearer token. On a 401, refreshes once and retries once."""

    def __init__(self, inner: Transport, credentials: OAuthCredentials) -> None:
        self.inner = inner
        self.credentials = credentials

    def _send(self, request: Request) -> Response:
        headers = {**request.headers, "Authorization": f"Bearer {self.credentials.token()}"}
        return self.inner.send(replace(request, headers=headers))

    def send(self, request: Request) -> Response:
        response = self._send(request)
        if response.status == 401:
            self.credentials.refresh()
            response = self._send(request)
            if response.status == 401:
                raise AuthError("Google refused the access token; reconnect the account")
        return response
