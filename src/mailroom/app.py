"""Composition: how the pieces are wired together, in one place."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .classify import LLMClassifier, RuleClassifier, llm_allowlist
from .config import Settings
from .defaults import number_patterns
from .documents import Expander
from .google.auth import AuthorizedTransport, OAuthCredentials
from .google.drive import GoogleDrive
from .google.gmail import GmailMailbox
from .google.policy import google_allowlist, is_idempotent
from .guard import GuardedTransport
from .http import RetryingTransport, Transport, UrllibTransport
from .ledger import Ledger
from .pipeline import Mailroom
from .split import NumberSplitter


def google_stack(wire: Transport, managed_folder: Callable[[str], bool], *,
                 credentials: Callable[[Transport], OAuthCredentials],
                 sleep: Callable[[float], None] = time.sleep,
                 ) -> tuple[Transport, GuardedTransport]:
    """auth -> retry -> allowlist guard -> wire. Every attempt, retries and token refreshes
    included, passes the guard. Returns the transport for adapters and the guard (its audit
    trail lists every request that was let through)."""
    guarded = GuardedTransport(wire, google_allowlist(managed_folder))
    retrying = RetryingTransport(guarded, idempotent=is_idempotent, sleep=sleep)
    return AuthorizedTransport(retrying, credentials(retrying)), guarded


@dataclass
class App:
    mailroom: Mailroom
    ledger: Ledger
    guard: GuardedTransport

    def close(self) -> None:
        self.ledger.close()


def build_app(settings: Settings, *, google_wire: Transport | None = None,
              llm_wire: Transport | None = None, env: Mapping[str, str] | None = None,
              sleep: Callable[[float], None] = time.sleep) -> App:
    """Everything `mailroom run` uses. The wires default to the real network; tests and the
    demo pass the in-process simulators instead and exercise exactly the same objects."""
    env = os.environ if env is None else env
    types = list(settings.types.values())
    rules = RuleClassifier(types, vendors=settings.vendors, date_order=settings.date_order)
    if settings.classifier == "llm":
        api_key = settings.api_key(env)  # fail before touching anything
        llm_transport = RetryingTransport(
            GuardedTransport(llm_wire or UrllibTransport(), llm_allowlist(settings.endpoint)),
            sleep=sleep)
        classifier: LLMClassifier | RuleClassifier = LLMClassifier(
            llm_transport, endpoint=settings.endpoint, model=settings.model,
            api_key=api_key, types=types)
        cross_check: RuleClassifier | None = rules
    else:
        classifier, cross_check = rules, None
    ledger = Ledger(settings.ledger_path, root_id=settings.root_folder_id)
    try:
        transport, guard = google_stack(
            google_wire or UrllibTransport(), ledger.managed_folder, sleep=sleep,
            credentials=lambda t: OAuthCredentials.from_file(settings.credentials_file,
                                                             transport=t))
    except Exception:
        ledger.close()
        raise
    splitter = NumberSplitter(number_patterns(dict(settings.types))) if settings.split else None
    mailroom = Mailroom(
        mailbox=GmailMailbox(transport, max_attachment_bytes=settings.max_attachment_bytes),
        drive=GoogleDrive(transport), ledger=ledger, classifier=classifier,
        policy=settings.policy(), expander=Expander(splitter=splitter), rules=cross_check,
        query=settings.query, allowed_senders=settings.allowed_senders,
        max_attempts=settings.max_attempts, max_upload_bytes=settings.max_upload_bytes)
    return App(mailroom, ledger, guard)
