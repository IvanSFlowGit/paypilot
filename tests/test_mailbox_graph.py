"""The read-only Microsoft Graph reader. Every request is recorded. SYNTHETIC mail only."""

from __future__ import annotations

from datetime import date

import pytest

from app.mailbox_graph import BASE, GraphAuthError, access_token, read_messages_graph

SINCE = date(2026, 11, 1)


def item(
    i="1",
    subject="Re: INV-1042",
    body="ok",
    when="2026-11-04T10:00:00Z",
    ctype="text",
    headers=None,
    frm="ap@client.example",
):
    return {
        "id": f"id{i}",
        "internetMessageId": f"<{i}@x>",
        "from": {"emailAddress": {"address": frm}},
        "toRecipients": [{"emailAddress": {"address": "b@us.example"}}],
        "subject": subject,
        "receivedDateTime": when,
        "body": {"contentType": ctype, "content": body},
        "internetMessageHeaders": headers or [],
    }


class Fake:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params, headers):
        self.calls.append((url, dict(params), dict(headers)))
        i = len([c for c in self.calls]) - 1
        if i >= len(self.pages):
            return 500, {}
        p = self.pages[i]
        return (p, {}) if isinstance(p, int) else (200, p)


def read(fake, **kw):
    return read_messages_graph(fake.get, "tok", SINCE, **kw)


def test_only_get_requests_under_the_graph_messages_path_with_bearer_and_text_preference():
    f = Fake([{"value": [item()]}])
    read(f)
    url, params, hdr = f.calls[0]
    assert url == BASE and hdr["Authorization"] == "Bearer tok" and "text" in hdr["Prefer"]
    assert params["$filter"].startswith("receivedDateTime ge 2026-11-01") and "$select" in params


def test_a_message_is_mapped_and_the_kept_headers_survive():
    f = Fake(
        [
            {
                "value": [
                    item(
                        headers=[
                            {"name": "Auto-Submitted", "value": "auto-replied"},
                            {"name": "X-Other", "value": "no"},
                        ]
                    )
                ]
            }
        ]
    )
    r = read(f)
    m = r.messages[0]
    assert (
        r.complete
        and m.from_addr == "ap@client.example"
        and m.headers == {"auto-submitted": "auto-replied"}
        and m.message_id == "<1@x>"
    )


def test_html_bodies_are_stripped_to_text():
    r = read(
        Fake([{"value": [item(body="<p>We will <b>pay</b> on 2026-11-14.</p>", ctype="html")]}])
    )
    assert "<" not in r.messages[0].body and "pay" in r.messages[0].body


def test_paging_follows_a_next_link_only_when_it_stays_on_the_graph_messages_path():
    nxt = BASE + "?$skiptoken=abc"
    f = Fake([{"value": [item("1")], "@odata.nextLink": nxt}, {"value": [item("2")]}])
    r = read(f)
    assert (
        len(r.messages) == 2
        and r.complete
        and f.calls[1][0] == nxt
        and f.calls[1][2]["Authorization"] == "Bearer tok"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/v1.0/me/messages?x=1",
        "https://graph.microsoft.com.evil.example/v1.0/me/messages",
        "https://graph.microsoft.com/v1.0/me/messagesXYZ",
        "https://graph.microsoft.com/v1.0/me/sendMail",
        "http://graph.microsoft.com/v1.0/me/messages",
        5,
    ],
)
def test_a_next_link_off_the_messages_path_is_refused_and_never_requested(bad):
    f = Fake([{"value": [item()], "@odata.nextLink": bad}, {"value": []}])
    r = read(f)
    assert (
        r.complete is False
        and r.folders_failed == ("graph",)
        and len(f.calls) == 1
        and len(r.messages) == 1
    )


def test_a_failed_list_is_incomplete_not_empty():
    r = read(Fake([403]))
    assert r.complete is False and r.messages == []


def test_a_bad_item_is_skipped_and_counted():
    r = read(Fake([{"value": [{"nonsense": 1}, item("2"), "str"]}]))
    assert r.skipped_unparseable == 2 and len(r.messages) == 1


def test_the_cap_marks_the_result_truncated_and_stops_paging():
    f = Fake(
        [
            {"value": [item("1"), item("2"), item("3")], "@odata.nextLink": BASE + "?p=2"},
            {"value": [item("4")]},
        ]
    )
    r = read(f, limit=2)
    assert len(r.messages) == 2 and r.truncated and len(f.calls) == 1


def test_old_mail_is_filtered_by_the_real_date():
    r = read(Fake([{"value": [item("1", when="2026-07-01T10:00:00Z"), item("2")]}]))
    assert len(r.messages) == 1


def posted(status=200, body=None):
    calls = []

    def post(url, data):
        calls.append((url, dict(data)))
        return status, body if body is not None else {
            "access_token": "at",
            "scope": "Mail.Read offline_access",
        }

    return post, calls


def test_the_token_exchange_posts_only_to_the_microsoft_token_endpoint_for_the_tenant():
    post, calls = posted()
    assert access_token(post, "contoso.onmicrosoft.com", "cid", "sec", "rt") == "at"
    assert (
        calls[0][0] == "https://login.microsoftonline.com/contoso.onmicrosoft.com/oauth2/v2.0/token"
    )
    assert "Mail.Read" in calls[0][1]["scope"] and "ReadWrite" not in calls[0][1]["scope"]


@pytest.mark.parametrize("tenant", ["", "a/b", "evil.example/../x", "x" * 80, "a b"])
def test_a_tenant_that_could_alter_the_url_is_refused_before_any_request(tenant):
    post, calls = posted()
    with pytest.raises(GraphAuthError):
        access_token(post, tenant, "c", "s", "r")
    assert calls == []


@pytest.mark.parametrize(
    "scope",
    [
        "Mail.ReadWrite",
        "Mail.Send offline_access",
        "Mail.Read Mail.ReadWrite",
        "Files.ReadWrite.All",
        "Mail.Read.All",
        "Mail.Read.Shared",
        "https://graph.microsoft.com/Mail.Read Mail.Send",
    ],
)
def test_a_token_with_any_scope_outside_the_allowlist_is_refused(scope):
    post, _ = posted(body={"access_token": "at", "scope": scope})
    with pytest.raises(GraphAuthError):
        access_token(post, "common", "c", "s", "r")


@pytest.mark.parametrize(
    "scope",
    [
        "Mail.Read offline_access",
        "mail.read",
        "Mail.Read openid profile email User.Read",
        "https://graph.microsoft.com/Mail.Read offline_access",
    ],
)
def test_the_expected_read_only_scopes_are_accepted(scope):
    post, _ = posted(body={"access_token": "at", "scope": scope})
    assert access_token(post, "common", "c", "s", "r") == "at"


def test_a_failed_exchange_raises_without_the_secret_or_body():
    post, _ = posted(
        status=401, body={"error": "invalid_client", "echo": "sec-xyz", "access_token": "at-leak"}
    )
    with pytest.raises(GraphAuthError) as e:
        access_token(post, "common", "cid", "sec-xyz", "rt")
    assert "sec-xyz" not in str(e.value) and "invalid_client" not in str(e.value)
