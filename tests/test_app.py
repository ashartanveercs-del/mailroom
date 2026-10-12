import json

import pytest

from mailroom.app import build_app
from mailroom.config import ConfigError, Settings
from mailroom.models import Attachment, Message
from mailroom.sim.google_api import CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, SimulatedGoogle
from mailroom.sim.model import Scripted, SimulatedModel
from mailroom.sim.pdfgen import make_pdf
from tests.support import T0

INVOICE = make_pdf([["Blue Fern Hosting", "INVOICE", "Invoice No: BF-2041",
                     "Invoice date: 03/14/2026", "Amount due: $120.00"]])
REPLY = {"doc_type": "invoice", "confidence": 0.95,
         "fields": {"vendor": "Blue Fern Hosting", "number": "BF-2041", "date": "2026-03-14"}}
PATH = "Invoices/2026/Blue Fern Hosting/2026-03-14 Blue Fern Hosting BF-2041.pdf"


def settings(tmp_path, classifier='model = "test-model"'):
    (tmp_path / "token.json").write_text(json.dumps({
        "type": "authorized_user", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN}))
    config = tmp_path / "mailroom.toml"
    config.write_text(f'[drive]\nroot_folder_id = "root"\n[classifier]\n{classifier}\n'
                      '[mailbox]\nallowed_senders = ["*@example.com"]\n')
    return Settings.load(config)


def world():
    message = Message("m1", "Billing <billing@example.com>", "March invoice", T0,
                      frozenset({"INBOX"}), (Attachment("BF-2041.pdf", "application/pdf",
                                                        INVOICE),))
    return SimulatedGoogle([message], root_id="root"), SimulatedModel([Scripted("BF-2041",
                                                                                REPLY)])


def test_the_production_wiring_files_end_to_end_over_simulated_services(tmp_path):
    google, model = world()
    app = build_app(settings(tmp_path), google_wire=google, llm_wire=model,
                    env={"MAILROOM_LLM_API_KEY": "sk-test"}, sleep=lambda s: None)
    report = app.mailroom.run()
    app.close()
    assert [o.path for o in report.outcomes] == [PATH]
    assert set(google.drive.tree()) == {PATH}
    assert model.requests[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert (tmp_path / "mailroom.db").exists()
    rules_used = {name for name, _, _ in app.guard.audit}
    assert rules_used <= {"gmail.messages.list", "gmail.messages.get",
                          "gmail.attachments.get", "drive.files.generateIds",
                          "drive.files.get", "drive.files.list", "drive.folders.create",
                          "drive.files.upload", "oauth.token.refresh"}


def test_preview_through_the_production_wiring_writes_nothing(tmp_path):
    google, model = world()
    app = build_app(settings(tmp_path), google_wire=google, llm_wire=model,
                    env={"MAILROOM_LLM_API_KEY": "sk-test"}, sleep=lambda s: None)
    report = app.mailroom.run(preview=True)
    app.close()
    assert [o.path for o in report.outcomes] == [PATH]
    assert google.drive.writes == 0
    assert not any(method == "POST" and "googleapis.com/" in url and "oauth2" not in url
                   for method, url in google.requests)


def test_the_rule_classifier_needs_no_model_or_key(tmp_path):
    google, model = world()
    app = build_app(settings(tmp_path, 'kind = "rules"\nvendors = ["Blue Fern Hosting"]'),
                    google_wire=google, llm_wire=model, env={}, sleep=lambda s: None)
    report = app.mailroom.run()
    app.close()
    assert [o.path for o in report.outcomes] == [PATH]
    assert model.requests == []


def test_a_missing_api_key_stops_before_anything_runs(tmp_path):
    google, model = world()
    with pytest.raises(ConfigError, match="MAILROOM_LLM_API_KEY"):
        build_app(settings(tmp_path), google_wire=google, llm_wire=model, env={})
    assert google.requests == []
