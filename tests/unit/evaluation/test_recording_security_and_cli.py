"""Recordings never carry credentials; the CLI never makes a live call
without an explicit, confirmed request. No database, no network."""

from __future__ import annotations

import json

import pytest

from packages.evaluation import cli
from packages.evaluation.recording import REDACTED, _scrub, known_secret_values

SECRET = "sk-ant-api03-" + "Z" * 60


def test_known_secret_values_come_from_env_and_dotenv(monkeypatch, tmp_path):
    # isolated directory: the real .env is never read here, and no assertion
    # below can print a list of secret values
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "FILE_ONLY_PASSWORD=file-secret-123\nANTHROPIC_API_KEY=from-file-1\n"
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-1234567890")
    monkeypatch.setenv("INVESTIGATION_MODEL", "claude-haiku-4-5")
    values = set(known_secret_values())
    found = {
        "env key": SECRET in values,
        "env token": "tok-1234567890" in values,
        "dotenv password": "file-secret-123" in values,
        "dotenv key (not overwritten by env)": "from-file-1" in values,
        "non-secret excluded": "claude-haiku-4-5" not in values,
    }
    assert all(found.values()), [k for k, ok in found.items() if not ok]


def test_scrubbing_redacts_known_values_and_credential_shapes():
    counter = [0]
    document = {
        "a": f"key={SECRET}",
        "b": ["Authorization: Bearer abcdefghijklmnop123"],
        "c": {"url": "postgresql+psycopg://user:hunter2@db:5432/x"},
        "d": "nothing secret here",
    }
    scrubbed = _scrub(document, [SECRET], counter)
    text = json.dumps(scrubbed)
    assert SECRET not in text and "hunter2" not in text and "abcdefghijklmnop123" not in text
    assert REDACTED in scrubbed["a"] and scrubbed["d"] == "nothing secret here"
    assert counter[0] >= 3


def test_live_evaluation_refuses_without_confirmation(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(cli, "_environment", lambda: pytest.fail("must not touch the DB"))
    assert cli.evaluate_main(["--scenario", "bad-deployment", "--mode", "live"]) == 2
    assert "--yes" in capsys.readouterr().err


def test_live_evaluation_refuses_without_credentials(monkeypatch, capsys):
    class NoKey:
        anthropic_api_key = None

    monkeypatch.setattr(cli, "AnthropicCredentials", NoKey)
    monkeypatch.setattr(cli, "_environment", lambda: pytest.fail("must not touch the DB"))
    code = cli.evaluate_main(["--scenario", "bad-deployment", "--mode", "live", "--yes"])
    assert code == 2
    assert "credentials" in capsys.readouterr().err


def test_live_evaluation_uses_the_configured_provider_and_model(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("INVESTIGATION_MODEL", "claude-haiku-4-5")
    monkeypatch.setattr(cli, "_environment", lambda: pytest.fail("must not touch the DB"))
    cli.evaluate_main(["--scenario", "bad-deployment", "--mode", "live", "--runs", "3"])
    out = capsys.readouterr().out
    assert "3 investigation(s) on anthropic/claude-haiku-4-5" in out
    cli.evaluate_main(
        ["--scenario", "bad-deployment", "--mode", "live", "--model", "claude-sonnet-4-6"]
    )
    assert "anthropic/claude-sonnet-4-6" in capsys.readouterr().out  # override, no code change


def test_unknown_provider_is_a_clean_configuration_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_environment", lambda: pytest.fail("must not touch the DB"))
    code = cli.evaluate_main(
        ["--scenario", "bad-deployment", "--mode", "live", "--provider", "nowhere", "--yes"]
    )
    assert code == 2 and "unknown investigation provider" in capsys.readouterr().err


def test_list_and_unknown_scenarios(capsys):
    assert cli.evaluate_main(["--list"]) == 0
    assert "bad-deployment" in capsys.readouterr().out
    assert cli.evaluate_main(["--scenario", "nope"]) == 2
