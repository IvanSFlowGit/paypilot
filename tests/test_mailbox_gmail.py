"""The read-only Gmail reader. Every request is recorded so 'read-only' is asserted. SYNTHETIC mail only."""

from __future__ import annotations

import base64
import re
from datetime import date
from email.message import EmailMessage

import pytest

from app.mailbox_gmail import (
    API,
    SCOPE,
    TOKEN_URL,
    GmailAuthError,
    access_token,
    read_messages_gmail,
)

SINCE = date(2026, 11, 1)
FORBIDDEN = re.compile(r"modify|send|delete|trash|batch|label|insert|import|drafts|watch", re.I)


def raw_mail(
    subject="Re: INV-1042", body="ok", date_hdr="Wed, 04 Nov 2026 10:00:00 +0000", extra=None
):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = (
        "ap@client.example",
        "b@us.example",
        subject,
        date_hdr,
        "<a@x>",
    )
    for k, v in (extra or {}).items():
        m[k] = v
    m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode().rstrip("=")


class Fake:
    def __init__(self, messages, sizes=None, list_status=200, fail_get_after=None, pages=None):
        self.messages, self.sizes, self.list_status = messages, sizes or {}, list_status
        self.fail_get_after, self.pages, self.calls = fail_get_after, pages, []

    def get(self, url, params, headers):
        self.calls.append(("GET", url, dict(params), dict(headers)))
        if url == API:
            if self.list_status != 200:
                return self.list_status, {}
            ids = list(self.messages)
            if self.pages:
                page = int(params.get("pageToken", "0"))
                chunk = ids[page * self.pages : (page + 1) * self.pages]
                nxt = {"nextPageToken": str(page + 1)} if (page + 1) * self.pages < len(ids) else {}
                return 200, {
                    "messages": [{"id": i} for i in chunk],
                    **nxt,
                    "nextLink": "https://evil.example/steal",
                }
            return 200, {"messages": [{"id": i} for i in ids]}
        mid = url.rsplit("/", 1)[1]
        if (
            self.fail_get_after is not None
            and sum(1 for c in self.calls if c[1] != API) > self.fail_get_after
        ):
            return 500, {}
        if params["format"] == "minimal":
            return 200, {"sizeEstimate": self.sizes.get(mid, 1000)}
        return 200, {"raw": self.messages[mid]}


def read(fake, **kw):
    return read_messages_gmail(fake.get, "tok123", SINCE, **kw)


def test_only_get_requests_on_the_two_message_endpoints_with_the_bearer_token():
    f = Fake({"a1": raw_mail()})
    read(f)
    assert f.calls and all(
        c[0] == "GET" and c[1].startswith(API) and c[3] == {"Authorization": "Bearer tok123"}
        for c in f.calls
    )
    assert not any(FORBIDDEN.search(c[1]) for c in f.calls)
    assert {c[2].get("format") for c in f.calls if c[1] != API} == {"minimal", "raw"}


def test_a_message_is_parsed_and_headers_survive():
    f = Fake({"a1": raw_mail(extra={"Auto-Submitted": "auto-replied"})})
    r = read(f)
    assert (
        r.complete
        and len(r.messages) == 1
        and r.messages[0].headers["auto-submitted"] == "auto-replied"
    )


def test_an_oversize_message_is_counted_and_never_downloaded():
    f = Fake({"a1": raw_mail(), "a2": raw_mail()}, sizes={"a2": 50_000_000})
    r = read(f)
    assert len(r.messages) == 1 and r.skipped_unparseable == 1
    assert not any(c[2].get("format") == "raw" and c[1].endswith("/a2") for c in f.calls)


def test_a_failed_list_is_an_incomplete_read_not_an_empty_one():
    r = read(Fake({}, list_status=403))
    assert r.complete is False and r.folders_failed == ("gmail",) and r.messages == []


def test_a_failure_mid_read_keeps_what_was_read_and_marks_incomplete():
    r = read(Fake({"a1": raw_mail(), "a2": raw_mail()}, fail_get_after=2))
    assert r.complete is False and len(r.messages) == 1


def test_paging_uses_the_page_token_and_never_follows_a_url_from_the_response():
    f = Fake({f"m{i}": raw_mail() for i in range(5)}, pages=2)
    r = read(f)
    assert len(r.messages) == 5 and r.complete
    assert not any("evil.example" in c[1] for c in f.calls)
    assert [c[2].get("pageToken") for c in f.calls if c[1] == API] == [None, "1", "2"]


def test_the_cap_marks_the_result_truncated():
    r = read(Fake({f"m{i}": raw_mail() for i in range(5)}), limit=2)
    assert len(r.messages) == 2 and r.truncated and not r.complete


def test_a_message_id_that_could_alter_the_path_is_skipped():
    f = Fake({"../../drafts": raw_mail(), "ok1": raw_mail()})
    r = read(f)
    assert len(r.messages) == 1 and r.skipped_unparseable == 1
    assert not any(".." in c[1] for c in f.calls)


def test_undecodable_raw_is_skipped_not_fatal():
    r = read(Fake({"a1": "!!!not base64!!!", "a2": raw_mail()}))
    assert len(r.messages) == 1


def test_old_mail_is_filtered_by_the_real_date():
    r = read(Fake({"a1": raw_mail(date_hdr="Wed, 01 Jul 2026 10:00:00 +0000"), "a2": raw_mail()}))
    assert len(r.messages) == 1


def posted(status=200, body=None):
    calls = []

    def post(url, data):
        calls.append((url, dict(data)))
        return status, body if body is not None else {"access_token": "at-1", "scope": SCOPE}

    return post, calls


def test_the_token_exchange_posts_only_to_googles_token_endpoint():
    post, calls = posted()
    assert access_token(post, "cid", "csecret", "rt") == "at-1"
    assert calls[0][0] == TOKEN_URL and calls[0][1]["grant_type"] == "refresh_token"


def test_a_failed_exchange_raises_without_the_secret_or_the_body():
    post, _ = posted(status=400, body={"error": "invalid_grant", "echo": "csecret-xyz"})
    with pytest.raises(GmailAuthError) as e:
        access_token(post, "cid", "csecret-xyz", "rt")
    assert "csecret-xyz" not in str(e.value) and "invalid_grant" not in str(e.value)


def test_a_token_with_more_than_the_read_only_scope_is_refused():
    post, _ = posted(body={"access_token": "at", "scope": SCOPE + " https://mail.google.com/"})
    with pytest.raises(GmailAuthError):
        access_token(post, "c", "s", "r")


def test_a_missing_access_token_is_refused():
    post, _ = posted(body={"scope": SCOPE})
    with pytest.raises(GmailAuthError):
        access_token(post, "c", "s", "r")


def test_once_the_cap_is_hit_no_further_page_is_requested():
    f = Fake({f"m{i}": raw_mail() for i in range(6)}, pages=2)
    r = read(f, limit=2)
    assert len(r.messages) == 2 and r.truncated
    # One more list call is needed to learn that more exist; it must then stop, not walk all pages.
    assert sum(1 for c in f.calls if c[1] == API) == 2


def test_a_non_200_exchange_is_refused_even_if_the_body_carries_a_token():
    post, _ = posted(status=500, body={"access_token": "at-leaked", "scope": SCOPE})
    with pytest.raises(GmailAuthError):
        access_token(post, "c", "s", "r")
