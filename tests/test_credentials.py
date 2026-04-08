"""Unit tests for the credentials module.

These cover the security-critical surface: encryption round-trip, fail-closed
behavior when no key is configured, placeholder substitution, and the
output redaction backstop.
"""

import pytest
from cryptography.fernet import Fernet

from rhiza_agents import credentials


@pytest.fixture(autouse=True)
def _set_encryption_key(monkeypatch):
    """Install a fresh encryption key for every test."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", key)
    credentials._fernet = None
    credentials._fernet_initialized = False
    yield
    credentials._fernet = None
    credentials._fernet_initialized = False


def test_credentials_disabled_when_key_unset(monkeypatch):
    monkeypatch.delenv("CREDENTIAL_ENCRYPTION_KEY", raising=False)
    credentials._fernet = None
    credentials._fernet_initialized = False
    assert credentials.credentials_enabled() is False
    with pytest.raises(RuntimeError, match="disabled"):
        credentials.encrypt_value("anything")
    with pytest.raises(RuntimeError, match="disabled"):
        credentials.decrypt_value(b"anything")


def test_encrypt_decrypt_roundtrip():
    plaintext = "hunter2"
    ct = credentials.encrypt_value(plaintext)
    assert plaintext.encode() not in ct  # not stored verbatim
    assert credentials.decrypt_value(ct) == plaintext


def test_encrypt_value_produces_different_ciphertext_each_call():
    """Fernet uses a random nonce; identical plaintexts encrypt differently."""
    a = credentials.encrypt_value("alice")
    b = credentials.encrypt_value("alice")
    assert a != b


def test_extract_placeholders():
    template = "machine x login {USER} password {PASSWD}\nliteral {NOT_A_VAR\n"
    placeholders = credentials.extract_placeholders(template)
    assert placeholders == ["USER", "PASSWD"]


def test_substitute_placeholders_preserves_braces():
    """Templates may contain literal braces (e.g. JSON) that aren't placeholders."""
    template = '{"key": "{TOKEN}", "other": "no_placeholder"}'
    out = credentials.substitute_placeholders(template, {"TOKEN": "abc123"})
    assert out == '{"key": "abc123", "other": "no_placeholder"}'


def test_substitute_unknown_placeholder_left_intact():
    out = credentials.substitute_placeholders("{KNOWN} and {UNKNOWN}", {"KNOWN": "yes"})
    assert out == "yes and {UNKNOWN}"


def test_redact_output_replaces_all_occurrences():
    text = "user=alice password=hunter2 retry=hunter2"
    out = credentials.redact_output(text, ["alice", "hunter2"])
    assert "alice" not in out
    assert "hunter2" not in out
    assert out.count("[REDACTED]") == 3


def test_redact_output_handles_empty():
    assert credentials.redact_output("", ["x"]) == ""
    assert credentials.redact_output("hello", []) == "hello"


def test_redact_output_dedupes_and_orders_by_length():
    """Longer values redact first so a containing value doesn't leave residue."""
    text = "abcdef and abc"
    out = credentials.redact_output(text, ["abc", "abcdef"])
    assert "abcdef" not in out
    assert out == "[REDACTED] and [REDACTED]"
