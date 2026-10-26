"""Where the identity key comes from: a systemd credential, a file, or the environment.

The credential is the shape the unit uses, so it has to work with the account JSON that
already exists (systemd copies the bytes) and with a bare key file, and every failure has
to name the source without ever printing the key.
"""

from __future__ import annotations

import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import cli


def key() -> str:
    return sdk.Keys.generate().secret_key().to_hex()


def credential(tmp_path, monkeypatch, body: str, name: str = "nostr_secret"):
    directory = tmp_path / "credentials"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(body)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(directory))
    return path


def test_a_credential_is_the_account_json(tmp_path, monkeypatch):
    secret = key()
    credential(tmp_path, monkeypatch, json.dumps({"secret_key_hex": secret}))
    assert cli.secret_from({"identity": {"secret_credential": "nostr_secret"}}) == secret


def test_a_credential_may_be_a_bare_key_with_a_newline(tmp_path, monkeypatch):
    secret = key()
    credential(tmp_path, monkeypatch, secret.upper() + "\n")
    assert cli.secret_from({"identity": {"secret_credential": "nostr_secret"}}) == secret


def test_an_nsec_credential_is_normalised_to_hex(tmp_path, monkeypatch):
    secret = key()
    nsec = sdk.SecretKey.parse(secret).to_bech32()
    credential(tmp_path, monkeypatch, nsec + "\n")
    assert cli.secret_from({"identity": {"secret_credential": "nostr_secret"}}) == secret


def test_the_credential_wins_over_a_file(tmp_path, monkeypatch):
    secret, other = key(), key()
    credential(tmp_path, monkeypatch, secret)
    path = tmp_path / "secret.json"
    path.write_text(json.dumps({"secret_key_hex": other}))
    cfg = {"identity": {"secret_credential": "nostr_secret", "secret_file": str(path)}}
    assert cli.secret_from(cfg) == secret


def test_no_credentials_directory_names_the_unit_setting(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(SystemExit) as caught:
        cli.secret_from({"identity": {"secret_credential": "nostr_secret"}})
    assert "LoadCredential" in str(caught.value)


def test_a_missing_credential_names_the_name_and_the_directory(tmp_path, monkeypatch):
    credential(tmp_path, monkeypatch, key(), name="other")
    with pytest.raises(SystemExit) as caught:
        cli.secret_from({"identity": {"secret_credential": "nostr_secret"}})
    message = str(caught.value)
    assert "nostr_secret" in message and "CREDENTIALS_DIRECTORY" in message


def test_an_invalid_key_names_its_source_and_not_the_value(tmp_path, monkeypatch):
    path = credential(tmp_path, monkeypatch, "not-a-key\n")
    with pytest.raises(SystemExit) as caught:
        cli.secret_from({"identity": {"secret_credential": "nostr_secret"}})
    message = str(caught.value)
    assert str(path) in message
    assert "not-a-key" not in message


def test_a_secret_file_still_works_and_is_normalised(tmp_path):
    secret = key()
    path = tmp_path / "secret.json"
    path.write_text(json.dumps({"secret_key": secret.upper()}))
    assert cli.secret_from({"identity": {"secret_file": str(path)}}) == secret


def test_a_missing_identity_names_all_three_options():
    with pytest.raises(SystemExit) as caught:
        cli.secret_from({"identity": {}})
    message = str(caught.value)
    assert all(option in message
               for option in ("secret_credential", "secret_env", "secret_file"))
