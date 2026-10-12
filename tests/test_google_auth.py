import json
import urllib.parse

import pytest

from mailroom.google.auth import AuthError, AuthorizedTransport, OAuthCredentials
from mailroom.http import Request, Response

API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class TokenServer:
    """Issues access tokens on refresh; API calls need the most recent one."""

    def __init__(self):
        self.issued = 0
        self.valid: set[str] = set()
        self.requests: list[Request] = []
        self.refuse = False

    def send(self, request):
        self.requests.append(request)
        if request.url.startswith("https://oauth2.googleapis.com/token"):
            if self.refuse:
                return Response(400, {}, b'{"error": "invalid_grant"}')
            self.issued += 1
            token = f"tok-{self.issued}"
            self.valid = {token}
            return Response(200, {}, json.dumps({"access_token": token, "expires_in": 3600,
                                                 "token_type": "Bearer"}).encode())
        if request.headers.get("Authorization", "").removeprefix("Bearer ") in self.valid:
            return Response(200, {}, b"{}")
        return Response(401, {}, b'{"error": {"code": 401}}')


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def setup(clock=None):
    server = TokenServer()
    creds = OAuthCredentials(client_id="cid", client_secret="csecret",
                             refresh_token="rtoken", transport=server, clock=clock or Clock())
    return server, creds, AuthorizedTransport(server, creds)


def test_first_call_refreshes_and_sends_the_bearer_token():
    server, _, api = setup()
    assert api.send(Request("GET", API)).status == 200
    refresh, call = server.requests
    form = dict(urllib.parse.parse_qsl(refresh.body.decode()))
    assert form == {"grant_type": "refresh_token", "client_id": "cid",
                    "client_secret": "csecret", "refresh_token": "rtoken"}
    assert "Authorization" not in refresh.headers
    assert call.headers["Authorization"] == "Bearer tok-1"


def test_the_access_token_is_reused_until_shortly_before_it_expires():
    clock = Clock()
    server, _, api = setup(clock)
    api.send(Request("GET", API))
    api.send(Request("GET", API))
    assert server.issued == 1
    clock.now += 3600 - 59
    api.send(Request("GET", API))
    assert server.issued == 2


def test_a_401_triggers_one_refresh_and_one_retry():
    server, _, api = setup()
    api.send(Request("GET", API))
    server.valid = set()                      # the token was revoked server-side
    assert api.send(Request("GET", API)).status == 200
    assert server.issued == 2


def test_a_second_401_is_an_auth_error():
    server, _, api = setup()
    server.send = lambda request, _orig=server.send: (
        _orig(request) if "oauth2" in request.url else Response(401, {}, b""))
    with pytest.raises(AuthError, match="reconnect"):
        api.send(Request("GET", API))


def test_a_refused_refresh_is_an_auth_error_without_secrets_in_it():
    server, _, api = setup()
    server.refuse = True
    with pytest.raises(AuthError) as err:
        api.send(Request("GET", API))
    assert "rtoken" not in str(err.value) and "csecret" not in str(err.value)


def test_credentials_load_from_an_authorized_user_file(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(json.dumps({"type": "authorized_user", "client_id": "cid",
                                "client_secret": "csecret", "refresh_token": "rtoken"}))
    creds = OAuthCredentials.from_file(path, transport=TokenServer())
    assert "rtoken" not in repr(creds) and "csecret" not in repr(creds)
    assert creds.token() == "tok-1"


@pytest.mark.parametrize("content", ["not json", "[]", '{"client_id": "x"}'])
def test_unusable_credential_files_are_auth_errors(tmp_path, content):
    path = tmp_path / "token.json"
    path.write_text(content)
    with pytest.raises(AuthError):
        OAuthCredentials.from_file(path, transport=TokenServer())


def test_a_missing_credential_file_is_an_auth_error(tmp_path):
    with pytest.raises(AuthError, match="not found"):
        OAuthCredentials.from_file(tmp_path / "nope.json", transport=TokenServer())
