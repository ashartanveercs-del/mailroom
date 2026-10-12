import pytest

from mailroom.guard import (
    Allowlist,
    BlockedRequest,
    GuardedTransport,
    PageCursor,
    Rule,
    UnsafePageToken,
)
from mailroom.http import Request, Response

ID = r"[A-Za-z0-9_-]+"


class Recorder:
    """Inner transport that records what actually reached the wire."""

    def __init__(self):
        self.sent: list[Request] = []

    def send(self, request: Request) -> Response:
        self.sent.append(request)
        return Response(200, {}, b"{}")


def no_body_over_10_bytes(request: Request) -> None:
    if request.body and len(request.body) > 10:
        raise BlockedRequest("body too large")


RULES = Allowlist([
    Rule("mail.list", "GET", "api.example.com", r"/mail/v1/messages",
         query={"q", "pageToken"}),
    Rule("mail.get", "GET", "api.example.com", rf"/mail/v1/messages/{ID}",
         query={"format"}, validators={"format": r"full|metadata"}),
    Rule("files.upload", "POST", "api.example.com", r"/upload/files",
         query={"fields"}, required={"uploadType": "multipart"}, body=no_body_over_10_bytes),
])


def guarded():
    inner = Recorder()
    return GuardedTransport(inner, RULES), inner


def test_allowed_request_reaches_the_wire_and_is_audited():
    transport, inner = guarded()
    resp = transport.send(Request("GET", "https://api.example.com/mail/v1/messages?q=has%3Aattachment"))
    assert resp.status == 200
    assert len(inner.sent) == 1
    assert transport.audit == [("mail.list", "GET", "/mail/v1/messages")]


@pytest.mark.parametrize("method,url,reason", [
    ("GET", "https://other.example.com/mail/v1/messages", "host"),
    ("GET", "https://api.example.com.attacker.example.com/mail/v1/messages", "host"),
    ("DELETE", "https://api.example.com/mail/v1/messages/abc", "not on the allowlist"),
    ("POST", "https://api.example.com/mail/v1/messages/abc/modify", "not on the allowlist"),
    ("GET", "http://api.example.com/mail/v1/messages", "https"),
    ("GET", "https://user@api.example.com/mail/v1/messages", "credentials"),
    ("GET", "https://api.example.com:8443/mail/v1/messages", "port"),
    ("GET", "https://api.example.com/mail/v1/messages#frag", "fragment"),
    ("GET", "https://api.example.com/mail/v1/messages/../batch", "path"),
    ("GET", "https://api.example.com/mail/v1//messages", "path"),
    ("GET", "https://api.example.com/mail/v1/messages/a%2Fb", "not on the allowlist"),
    ("GET", "https://api.example.com/mail/v1/messages?alt=media", "query parameter 'alt'"),
    ("GET", "https://api.example.com/mail/v1/messages?q=a&q=b", "repeated"),
    ("GET", "https://api.example.com/mail/v1/messages/abc?format=raw", "format"),
    ("POST", "https://api.example.com/upload/files?uploadType=resumable", "uploadType"),
    ("POST", "https://api.example.com/upload/files", "uploadType"),
])
def test_requests_off_the_allowlist_never_reach_the_wire(method, url, reason):
    transport, inner = guarded()
    with pytest.raises(BlockedRequest, match=reason):
        transport.send(Request(method, url))
    assert inner.sent == []
    assert transport.audit == []


def test_host_comparison_ignores_case_but_nothing_else():
    transport, inner = guarded()
    transport.send(Request("GET", "https://API.Example.com/mail/v1/messages"))
    assert len(inner.sent) == 1


def test_explicit_default_port_is_fine():
    transport, inner = guarded()
    transport.send(Request("GET", "https://api.example.com:443/mail/v1/messages"))
    assert len(inner.sent) == 1


@pytest.mark.parametrize("header", ["X-HTTP-Method-Override", "x-http-method", "X-Method-Override"])
def test_method_override_headers_are_refused(header):
    # Several HTTP APIs execute a POST as PATCH/DELETE when this header is present, which would
    # turn an allowlisted method into a different one behind the guard's back.
    transport, inner = guarded()
    with pytest.raises(BlockedRequest, match="method override"):
        transport.send(Request("GET", "https://api.example.com/mail/v1/messages",
                               headers={header: "DELETE"}))
    assert inner.sent == []


def test_body_validator_runs_before_sending():
    transport, inner = guarded()
    url = "https://api.example.com/upload/files?uploadType=multipart"
    transport.send(Request("POST", url, body=b"small"))
    with pytest.raises(BlockedRequest, match="body too large"):
        transport.send(Request("POST", url, body=b"x" * 50))
    assert len(inner.sent) == 1


def test_describe_lists_every_rule():
    rows = RULES.describe()
    assert [r["name"] for r in rows] == ["mail.list", "mail.get", "files.upload"]
    upload = rows[2]
    assert upload["method"] == "POST" and upload["host"] == "api.example.com"
    assert "uploadType=multipart" in upload["query"]


def test_rule_with_unknown_method_is_rejected_at_definition():
    with pytest.raises(ValueError):
        Rule("bad", "get", "api.example.com", r"/x")


# --- pagination ---------------------------------------------------------------------------

def test_page_cursor_accepts_opaque_tokens_and_stops_at_the_end():
    cursor = PageCursor()
    assert cursor.advance("09876543210") == "09876543210"
    assert cursor.advance("~!!~AI9FV7Q-x_y.z=") == "~!!~AI9FV7Q-x_y.z="
    assert cursor.advance(None) is None
    assert cursor.advance("") is None


@pytest.mark.parametrize("token", [
    "https://attacker.example.com/next",
    "abc&uploadType=resumable",
    "abc def",
    "abc\r\nX-Injected: 1",
    "a" * 3000,
    12345,
    {"url": "https://attacker.example.com"},
])
def test_page_cursor_rejects_tokens_that_are_not_opaque_tokens(token):
    with pytest.raises(UnsafePageToken):
        PageCursor().advance(token)


def test_page_cursor_rejects_a_repeated_token():
    cursor = PageCursor()
    cursor.advance("page2")
    cursor.advance("page3")
    with pytest.raises(UnsafePageToken, match="repeated"):
        cursor.advance("page2")


def test_page_cursor_caps_the_number_of_pages():
    cursor = PageCursor(max_pages=3)
    cursor.advance("p1")
    cursor.advance("p2")
    with pytest.raises(UnsafePageToken, match="more than 3 pages"):
        cursor.advance("p3")
