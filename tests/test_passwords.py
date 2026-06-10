from __future__ import annotations

import pytest

from services.passwords import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    encoded = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", encoded) is True
    assert verify_password("wrong", encoded) is False


def test_hash_is_salted_so_identical_passwords_differ():
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first)
    assert verify_password("same-password", second)


def test_encoded_format_is_self_describing():
    encoded = hash_password("pw")
    parts = encoded.split("$")
    assert len(parts) == 4
    algorithm, iterations, salt, digest = parts
    assert algorithm == "pbkdf2_sha256"
    assert int(iterations) > 0
    assert salt and digest


def test_empty_password_is_rejected():
    with pytest.raises(ValueError):
        hash_password("")


def test_malformed_encoding_returns_false_without_raising():
    assert verify_password("pw", "not-a-valid-hash") is False
    assert verify_password("pw", "") is False
    # Unknown algorithm prefix is rejected rather than trusted.
    assert verify_password("pw", "bcrypt$12$abc$def") is False
    # Non-hex salt/digest must not raise.
    assert verify_password("pw", "pbkdf2_sha256$600000$zz$zz") is False
