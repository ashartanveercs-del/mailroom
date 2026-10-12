import textwrap
from pathlib import Path

import pytest

from mailroom.config import ConfigError, Settings
from mailroom.defaults import DEFAULT_TYPES
from mailroom.pipeline import DEFAULT_QUERY

MINIMAL = """
[drive]
root_folder_id = "1AbCdEfGhIjK"

[classifier]
model = "some-model"
"""


def load(tmp_path: Path, text: str) -> Settings:
    path = tmp_path / "mailroom.toml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return Settings.load(path)


def test_minimal_config_uses_safe_defaults(tmp_path):
    s = load(tmp_path, MINIMAL)
    assert s.root_folder_id == "1AbCdEfGhIjK"
    assert s.types == DEFAULT_TYPES
    assert s.review_folder == "Needs review"
    assert s.classifier == "llm" and s.cross_check and s.grounding
    assert s.query == DEFAULT_QUERY
    assert s.ledger_path == tmp_path / "mailroom.db"           # relative to the config file
    assert s.credentials_file == tmp_path / "token.json"
    assert s.max_upload_bytes == 5 * 1024 * 1024


def test_extra_search_terms_are_added_to_the_safety_exclusions(tmp_path):
    s = load(tmp_path, MINIMAL + '\n[mailbox]\nquery = "label:bills"\n')
    assert s.query == DEFAULT_QUERY + " label:bills"


def test_document_types_replace_the_defaults(tmp_path):
    s = load(tmp_path, MINIMAL + """
[types.statement]
description = "A monthly bank statement"
folder = "Bank/{date:%Y}"
filename = "{date:%Y-%m} statement"
keywords = ["statement", "opening balance"]
min_confidence = 0.9
""")
    assert list(s.types) == ["statement"]
    spec = s.types["statement"]
    assert spec.keywords == ("statement", "opening balance") and spec.min_confidence == 0.9


@pytest.mark.parametrize("snippet,message", [
    ("[drive]\n", "root_folder_id"),
    ('[drive]\nroot_folder_id = "../x"\n', "root_folder_id"),
    (MINIMAL + "\n[mailbox]\nquerry = 'typo'\n", "querry"),
    (MINIMAL + "\n[surprise]\n", "surprise"),
    (MINIMAL.replace('model = "some-model"', 'model = "m"\nkind = "magic"'), "kind"),
    (MINIMAL.replace('model = "some-model"', 'model = "m"\nendpoint = "http://x.example.com/v1"'),
     "https"),
    (MINIMAL.replace('model = "some-model"', ""), "model"),
    (MINIMAL + '\n[types.x]\ndescription = "x"\nfolder = "{amount}"\nfilename = "x"\n',
     "amount"),
    (MINIMAL + '\n[types.x]\ndescription = "x"\nfolder = "X"\nfilename = "x"\n'
               'number_pattern = "("\n', "number_pattern"),
    (MINIMAL + '\n[classifier.extra]\n', "extra"),
    ("this is not toml [", "TOML"),
])
def test_bad_configs_fail_at_startup_with_a_clear_message(tmp_path, snippet, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, snippet)


def test_the_owners_own_names_reach_the_decision_policy(tmp_path):
    s = load(tmp_path, MINIMAL + 'owner_names = ["Larkspur Design Studio", "Larkspur LLC"]\n')
    assert s.policy().owner_names == ("Larkspur Design Studio", "Larkspur LLC")
    assert load(tmp_path, MINIMAL).policy().owner_names == ()
    with pytest.raises(ConfigError, match="owner_names"):
        load(tmp_path, MINIMAL + 'owner_names = "Larkspur Design Studio"\n')


def test_rules_classifier_needs_no_model(tmp_path):
    s = load(tmp_path, '[drive]\nroot_folder_id = "abc"\n[classifier]\nkind = "rules"\n')
    assert s.classifier == "rules"


def test_api_key_is_read_from_the_named_environment_variable(tmp_path):
    s = load(tmp_path, MINIMAL)
    assert s.api_key({"MAILROOM_LLM_API_KEY": "sk-from-env"}) == "sk-from-env"
    with pytest.raises(ConfigError, match="MAILROOM_LLM_API_KEY"):
        s.api_key({})


def test_a_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Settings.load(tmp_path / "nope.toml")


def test_the_example_config_loads_and_spells_out_exactly_the_defaults():
    example = Path(__file__).resolve().parent.parent / "mailroom.example.toml"
    s = Settings.load(example)
    assert s.types == DEFAULT_TYPES
    assert s.classifier == "llm" and s.model and s.cross_check and s.grounding
