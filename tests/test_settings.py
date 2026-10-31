"""Settings store, Fernet SecretBox and key masking (ported from IPLens's test_settings)."""

import os
import stat

import pytest

from tflens.crypto import LazySecretBox, SecretBox
from tflens.settings import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_RETENTION_RUNS,
    Settings,
    SettingsStore,
    effective_server,
    mask_key_id,
)

FAKE_KEY_ID = "AKIAEXAMPLEEXAMPLE00"


@pytest.fixture
def store(db_path):
    return SettingsStore(db_path)


def test_defaults(store):
    s = store.load()
    assert (s.log_dir, s.host, s.port, s.allow_remote) == ("", DEFAULT_HOST, DEFAULT_PORT, False)
    assert (s.auto_regenerate, s.retention_runs) == (False, DEFAULT_RETENTION_RUNS)
    assert store.active_account_id() is None


def test_defaults_without_database_create_nothing(tmp_path):
    path = tmp_path / "nested" / "tflens.db"
    store = SettingsStore(path)
    assert store.load() == Settings()
    assert store.active_account_id() is None
    assert not path.exists() and not path.parent.exists()


def test_first_save_creates_the_database(tmp_path):
    path = tmp_path / "tflens.db"
    SettingsStore(path).save(log_dir="/srv/example-logs")
    assert path.is_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert SettingsStore(path).load().log_dir == "/srv/example-logs"


def test_log_dir_round_trip(store):
    store.save(log_dir="  /srv/example-logs ")
    assert store.load().log_dir == "/srv/example-logs"


def test_server_and_run_settings_round_trip(store):
    store.save(
        host="0.0.0.0",
        port="9000",
        allow_remote=True,
        auto_regenerate=True,
        retention_runs="5",
    )
    s = store.load()
    assert (s.host, s.port, s.allow_remote, s.auto_regenerate, s.retention_runs) == (
        "0.0.0.0",
        9000,
        True,
        True,
        5,
    )
    store.save(log_dir="")  # None keeps the other values
    assert store.load().port == 9000


@pytest.mark.parametrize(
    "kwargs, msg",
    [
        ({"port": "70000"}, "port"),
        ({"port": "abc"}, "port"),
        ({"retention_runs": "0"}, "run retention"),
        ({"host": "http://evil.example/"}, "host"),
    ],
)
def test_invalid_values_are_rejected_and_nothing_is_written(store, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        store.save(log_dir="/srv/new", **kwargs)
    assert store.load().log_dir == ""


def test_cli_flags_override_settings():
    s = Settings(host="192.0.2.10", port=9000, allow_remote=False)
    assert effective_server(s, None, None, False) == ("192.0.2.10", 9000, False)
    assert effective_server(s, "127.0.0.1", 8080, True) == ("127.0.0.1", 8080, True)
    assert effective_server(Settings(allow_remote=True), None, 0, False) == (
        DEFAULT_HOST,
        0,
        True,
    )


def test_active_account_round_trip(store):
    store.set_active_account_id(7)
    assert store.active_account_id() == 7
    store.set_active_account_id(None)
    assert store.active_account_id() is None


def test_key_file_permissions(tmp_path):
    path = tmp_path / "k" / "secret.key"
    box = SecretBox.from_path(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    token = box.encrypt("hello")
    assert SecretBox.from_path(path).decrypt(token) == "hello"
    assert "redacted" in repr(box)


def test_lazy_box_creates_the_key_on_first_use(tmp_path):
    path = tmp_path / "secret.key"
    box = LazySecretBox(path)
    assert not path.exists()
    token = box.encrypt("hello")
    assert path.is_file() and SecretBox.from_path(path).decrypt(token) == "hello"
    assert "redacted" in repr(box)


def test_env_key_overrides_file(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TFLENS_SECRET_KEY", key)
    box = SecretBox.from_path(tmp_path / "unused.key")
    assert not (tmp_path / "unused.key").exists()
    other = SecretBox(Fernet.generate_key())
    with pytest.raises(ValueError):
        other.decrypt(box.encrypt("x"))


def test_mask_key_id():
    assert mask_key_id("") == ""
    assert mask_key_id("short") == "*****"
    masked = mask_key_id(FAKE_KEY_ID)
    assert masked == "AKIA" + "*" * 12 + "LE00"
