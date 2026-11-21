# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
"""The one place a mailbox reader is built from the environment.

Used by scripts/mailbox_facts_export.py (invoice facts), scripts/watch_replies.py and the in-app
reply watch (app/reply_watch.py), so the three cannot disagree about which credentials a provider
needs. Read-only on every provider; credentials come from the environment and nowhere else.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date

from app import mailbox_gmail, mailbox_graph
from app.mailbox_imap import ReadResult, connect_from_config, read_messages

PROVIDERS = ("imap", "gmail", "graph")


def reader_for(provider: str, folders: list[str] | tuple[str, ...] = ("INBOX",), connect=None,
               config=None) -> Callable[[date], ReadResult]:
    """``read(since) -> ReadResult`` for the provider. Raises ValueError when credentials are missing."""
    env = os.environ
    if provider == "gmail":
        if config is not None and getattr(config, "gmail_read_refresh_token", ""):
            cid, sec, ref = config.gmail_client_id, config.gmail_client_secret, config.gmail_read_refresh_token
        else:
            cid, sec, ref = (env.get("GMAIL_CLIENT_ID", ""), env.get("GMAIL_CLIENT_SECRET", ""),
                             env.get("GMAIL_REFRESH_TOKEN", ""))
        if not (cid and sec and ref):
            raise ValueError("gmail: set GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN "
                             "(or the tenant's gmail_client_id, gmail_client_secret, gmail_read_refresh_token)")
        tok = mailbox_gmail.access_token(mailbox_gmail.real_post, cid, sec, ref)
        return lambda since: mailbox_gmail.read_messages_gmail(mailbox_gmail.real_get, tok, since)
    if provider == "graph":
        if config is not None and getattr(config, "ms_refresh_token", ""):
            vals = (config.ms_tenant, config.ms_client_id, config.ms_client_secret, config.ms_refresh_token)
        else:
            vals = tuple(env.get(k, "") for k in ("MS_TENANT", "MS_CLIENT_ID", "MS_CLIENT_SECRET", "MS_REFRESH_TOKEN"))
        if not all(vals):
            raise ValueError("graph: set MS_TENANT, MS_CLIENT_ID, MS_CLIENT_SECRET, MS_REFRESH_TOKEN "
                             "(or the tenant's ms_* secrets)")
        tok = mailbox_graph.access_token(mailbox_graph.real_post, *vals)
        return lambda since: mailbox_graph.read_messages_graph(mailbox_graph.real_get, tok, since)
    if provider == "imap":
        return lambda since: read_messages(connect or connect_from_config(config), tuple(folders), since)
    raise ValueError(f"unknown mailbox provider {provider!r}")
