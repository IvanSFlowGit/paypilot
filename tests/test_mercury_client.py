"""The Mercury read client: GET only, bounded retries, the token never leaves."""

from __future__ import annotations

import inspect
import json

import pytest

from app import mercury_client as mc

TOKEN = "secret-token-never-print-9f3"


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        item = self.responses.pop(0) if self.responses else (200, b"[]")
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses, sleeps=None, **kw):
    fake = Fake(*responses)
    rec = [] if sleeps is None else sleeps
    c = mc.MercuryReadClient(TOKEN, transport=fake, sleep=rec.append, **kw)
    return c, fake, rec


def ok(obj):
    return 200, json.dumps(obj).encode()


def test_only_get_is_ever_issued_and_the_url_and_headers_are_as_documented():
    c, fake, _ = client(ok([]))
    c.list_invoices()
    method, url, headers, timeout = fake.calls[0]
    assert method == "GET" and url == "https://api.mercury.com/api/v1/ar/invoices"
    assert headers["Authorization"] == "Bearer " + TOKEN and timeout == mc.TIMEOUT_SECONDS


def test_the_class_exposes_no_write_path():
    public = {n for n, _ in inspect.getmembers(mc.MercuryReadClient, inspect.isfunction) if not n.startswith("_")}
    assert public == {"list_invoices", "get_invoice"}
    src = inspect.getsource(mc)
    for verb in ('"POST"', '"PUT"', '"PATCH"', '"DELETE"'):
        assert verb not in src


def test_the_default_transport_refuses_anything_but_get(monkeypatch):
    def no_socket(*a, **k):
        raise AssertionError("a non-GET reached the network")

    monkeypatch.setattr(mc.urllib.request, "urlopen", no_socket)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(mc.MercuryError):
            mc.default_transport(method, "https://api.mercury.com/x", {}, 1)


def test_the_token_is_not_in_repr_or_any_error():
    c, _, _ = client()
    assert TOKEN not in repr(c) and "redacted" in repr(c)
    for status, exc in [(401, mc.MercuryAuthError), (403, mc.MercuryAuthError), (404, mc.MercuryNotFound),
                        (429, mc.MercuryRateLimited), (500, mc.MercuryServerError), (418, mc.MercuryError)]:
        c, _, _ = client(*[(status, b"body " + TOKEN.encode())] * 3)
        with pytest.raises(exc) as e:
            c.get_invoice("inv_1")
        assert TOKEN not in str(e.value)


def test_no_token_is_refused_and_the_env_is_the_default(monkeypatch):
    monkeypatch.delenv(mc.TOKEN_ENV, raising=False)
    for bad in ("", "   "):
        with pytest.raises(mc.MercuryAuthError):
            mc.MercuryReadClient(bad, transport=Fake())
    with pytest.raises(mc.MercuryAuthError):
        mc.MercuryReadClient(transport=Fake())
    monkeypatch.setenv(mc.TOKEN_ENV, TOKEN)
    assert mc.MercuryReadClient(transport=Fake()) is not None


def test_a_non_https_base_is_refused():
    with pytest.raises(mc.MercuryError):
        mc.MercuryReadClient(TOKEN, base_url="http://api.mercury.com/api/v1", transport=Fake())


def test_a_429_is_retried_with_backoff_then_succeeds():
    c, fake, sleeps = client((429, b""), (503, b""), ok([{"id": "a"}]))
    assert c.list_invoices().items == [{"id": "a"}]
    assert len(fake.calls) == 3 and sleeps == [1, 2]


def test_retries_are_bounded_and_the_last_status_decides_the_error():
    c, fake, sleeps = client((429, b""), (429, b""), (429, b""), (200, b"[]"))
    with pytest.raises(mc.MercuryRateLimited):
        c.list_invoices()
    assert len(fake.calls) == mc.MAX_ATTEMPTS and len(sleeps) == mc.MAX_ATTEMPTS - 1


def test_a_transport_timeout_is_retried_then_a_server_error():
    c, fake, _ = client(TimeoutError(), OSError(), TimeoutError())
    with pytest.raises(mc.MercuryServerError):
        c.list_invoices()
    assert len(fake.calls) == 3


@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_client_errors_are_never_retried(status):
    c, fake, _ = client((status, b""), ok([]))
    with pytest.raises(mc.MercuryError):
        c.get_invoice("inv_1")
    assert len(fake.calls) == 1


def test_the_list_is_never_reported_complete():
    c, _, _ = client(ok([{"id": "a"}, {"id": "b"}]))
    r = c.list_invoices()
    assert r.complete == "unknown" and len(r.items) == 2


def test_an_enveloped_list_needs_the_key_and_a_wrong_shape_is_an_error():
    c, _, _ = client(ok({"invoices": [{"id": "a"}]}))
    assert c.list_invoices(items_key="invoices").items == [{"id": "a"}]
    for body, key in [({"invoices": []}, None), ([], "invoices"), ({"other": []}, "invoices"),
                      ({"invoices": "x"}, "invoices")]:
        c, _, _ = client(ok(body))
        with pytest.raises(mc.MercuryResponseError):
            c.list_invoices(items_key=key)


def test_non_json_and_non_object_bodies_are_response_errors():
    c, _, _ = client((200, b"<html>"))
    with pytest.raises(mc.MercuryResponseError):
        c.list_invoices()
    c, _, _ = client(ok([1]))
    with pytest.raises(mc.MercuryResponseError):
        c.get_invoice("inv_1")


@pytest.mark.parametrize("bad", ["", "../accounts", "a/b", "a b", "a?x=1", None, 5])
def test_an_invoice_id_cannot_escape_the_path(bad):
    c, fake, _ = client()
    with pytest.raises(mc.MercuryError):
        c.get_invoice(bad)
    assert fake.calls == []
