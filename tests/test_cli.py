import json

from mailroom.__main__ import main


def test_allowlist_lists_every_request_mailroom_may_make(capsys):
    assert main(["allowlist"]) == 0
    out = capsys.readouterr().out
    for name in ("gmail.messages.list", "gmail.messages.get", "gmail.attachments.get",
                 "drive.files.generateIds", "drive.files.get", "drive.files.list",
                 "drive.folders.create", "drive.files.upload", "oauth.token.refresh",
                 "llm.chat_completions"):
        assert name in out
    assert "DELETE" not in out and "PATCH" not in out and "PUT" not in out


def test_eval_passes_exits_zero_and_writes_a_json_report(capsys, tmp_path):
    report = tmp_path / "report.json"
    code = main(["eval", "--corpus", "everyday", "--transport", "memory", "--mode", "live",
                 "--json", str(report)])
    assert code == 0
    assert "GATE PASSED" in capsys.readouterr().out
    data = json.loads(report.read_text())
    assert data["passed"] and data["totals"]["misfiles"] == 0


def test_eval_fails_with_exit_one_when_safety_checks_are_switched_off(capsys):
    code = main(["eval", "--corpus", "adversarial", "--transport", "memory", "--mode", "live",
                 "--disable", "cross_check", "--disable", "grounding"])
    out = capsys.readouterr().out
    assert code == 1
    assert "GATE FAILED" in out and "MISFILE" in out


def test_demo_runs_a_corpus_through_the_real_adapters_offline(capsys):
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Invoices/2026/Blue Fern Hosting/2026-03-14 Blue Fern Hosting BF-2041.pdf" in out
    assert "Needs review/spring-catalogue.pdf" in out and "unrecognized_type" in out
    assert "drive.files.upload" in out          # the requests the guard let through


def test_demo_json(capsys):
    assert main(["demo", "--corpus", "containers", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["filed"] >= 9


def test_run_with_a_missing_config_exits_two(tmp_path, capsys):
    assert main(["run", "--config", str(tmp_path / "missing.toml")]) == 2
    assert "not found" in capsys.readouterr().err


def test_run_without_the_api_key_exits_two_before_contacting_anything(tmp_path, capsys):
    config = tmp_path / "mailroom.toml"
    config.write_text('[drive]\nroot_folder_id = "abc"\n[classifier]\nmodel = "m"\n')
    assert main(["run", "--config", str(config)], env={}) == 2
    assert "MAILROOM_LLM_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "mailroom.db").exists()


def test_run_reads_the_api_key_from_an_env_file(tmp_path, capsys):
    config = tmp_path / "mailroom.toml"
    config.write_text('[drive]\nroot_folder_id = "abc"\n[classifier]\nmodel = "m"\n')
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nMAILROOM_LLM_API_KEY='sk-from-file'\n")
    assert main(["run", "--config", str(config), "--env-file", str(env_file)], env={}) == 2
    err = capsys.readouterr().err
    assert "credential file not found" in err      # got past the API key check
    assert "sk-from-file" not in err


def test_run_with_missing_google_credentials_exits_two(tmp_path, capsys):
    config = tmp_path / "mailroom.toml"
    config.write_text('[drive]\nroot_folder_id = "abc"\n[classifier]\nkind = "rules"\n')
    assert main(["run", "--config", str(config)], env={}) == 2
    assert "credential file not found" in capsys.readouterr().err
