"""The examples are part of the documentation, so they are run like any other test."""

import runpy
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run(name, capsys):
    runpy.run_path(str(EXAMPLES / name), run_name="__main__")
    return capsys.readouterr().out


def test_offline_quickstart(capsys):
    out = run("offline_quickstart.py", capsys)
    assert "filed   Bank statements/2026/2026-03 Example Credit Union statement.pdf" in out
    assert "review  Needs review/lunch-menu.pdf (unrecognized_type)" in out


def test_custom_splitter(capsys):
    out = run("custom_splitter.py", capsys)
    assert "msg-1/binder.pdf#p1-2" in out and "msg-1/binder.pdf#p4" in out
