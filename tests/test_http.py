import pytest

from mailroom.http import Request, Response, RetryingTransport, TransportError


class Scripted:
    """Answers each request with the next scripted item (a Response or an exception)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def send(self, request):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok():
    return Response(200, {}, b'{"ok": true}')


def test_retries_throttling_and_server_errors_with_backoff():
    sleeps = []
    inner = Scripted(Response(429, {}, b""), Response(503, {}, b""), ok())
    transport = RetryingTransport(inner, sleep=sleeps.append, base_delay=0.5)
    assert transport.send(Request("GET", "https://x.example.com/")).json() == {"ok": True}
    assert inner.calls == 3
    assert sleeps == [0.5, 1.0]


def test_honours_retry_after_but_caps_it():
    sleeps = []
    inner = Scripted(Response(429, {"Retry-After": "7"}, b""),
                     Response(429, {"retry-after": "9999"}, b""), ok())
    RetryingTransport(inner, sleep=sleeps.append).send(Request("GET", "https://x.example.com/"))
    assert sleeps == [7.0, 60.0]


def test_gives_up_after_the_last_attempt_and_returns_the_error_response():
    inner = Scripted(*[Response(500, {}, b"boom")] * 3)
    resp = RetryingTransport(inner, attempts=3, sleep=lambda s: None).send(
        Request("GET", "https://x.example.com/"))
    assert resp.status == 500 and inner.calls == 3


def test_client_errors_are_not_retried():
    inner = Scripted(Response(404, {}, b""))
    resp = RetryingTransport(inner, sleep=lambda s: None).send(
        Request("GET", "https://x.example.com/"))
    assert resp.status == 404 and inner.calls == 1


def test_network_errors_are_retried_only_for_idempotent_requests():
    inner = Scripted(TransportError("reset"), ok())
    RetryingTransport(inner, sleep=lambda s: None).send(Request("GET", "https://x.example.com/"))
    assert inner.calls == 2

    inner = Scripted(TransportError("reset"), ok())
    with pytest.raises(TransportError):
        RetryingTransport(inner, sleep=lambda s: None).send(
            Request("POST", "https://x.example.com/", body=b"{}"))
    assert inner.calls == 1


def test_custom_idempotency_predicate_allows_safe_posts_to_be_retried():
    inner = Scripted(TransportError("reset"), ok())
    transport = RetryingTransport(inner, sleep=lambda s: None,
                                  idempotent=lambda req: req.headers.get("X-Idempotent") == "1")
    transport.send(Request("POST", "https://x.example.com/", headers={"X-Idempotent": "1"}))
    assert inner.calls == 2


def test_response_header_lookup_is_case_insensitive():
    resp = Response(200, {"Content-Type": "application/json"}, b"")
    assert resp.header("content-type") == "application/json"
    assert resp.header("missing") is None
    assert resp.json() == {}
