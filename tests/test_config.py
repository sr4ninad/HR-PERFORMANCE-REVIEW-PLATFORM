import pytest

from app import config


def test_production_refuses_to_start_without_secret_key(monkeypatch):
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.delenv("HR_REVIEW_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="must be set"):
        config.load_secret_key()


def test_production_rejects_short_secret_key(monkeypatch):
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.setenv("HR_REVIEW_SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="at least"):
        config.load_secret_key()


def test_dev_generates_and_persists_a_random_key(monkeypatch, tmp_path):
    key_file = tmp_path / ".secret_key"
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    monkeypatch.setattr(config, "SECRET_KEY_FILE", key_file)
    monkeypatch.delenv("HR_REVIEW_SECRET_KEY", raising=False)

    first = config.load_secret_key()
    second = config.load_secret_key()  # e.g. a restart, or a second worker

    assert len(first) >= config.MIN_SECRET_KEY_LENGTH
    assert first == second == key_file.read_text().strip()


def test_env_key_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SECRET_KEY_FILE", tmp_path / ".secret_key")
    monkeypatch.setenv("HR_REVIEW_SECRET_KEY", "x" * 40)
    assert config.load_secret_key() == "x" * 40
    assert not (tmp_path / ".secret_key").exists()
