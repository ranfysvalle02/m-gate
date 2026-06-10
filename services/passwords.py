from __future__ import annotations

import hashlib
import hmac
import secrets

# Password hashing uses PBKDF2-HMAC-SHA256 from the standard library. This keeps
# the gateway free of a new (binary) dependency — so the offline test tier and
# slim container image are unaffected — while still meeting OWASP's 2023 guidance
# for PBKDF2 work factor. The stored value is self-describing
# (``algorithm$iterations$salt$hash``), so the algorithm or work factor can be
# upgraded later (e.g. to argon2/bcrypt) with transparent rehash-on-verify.
_ALGORITHM = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 600_000
_SALT_BYTES = 16
_DERIVED_KEY_BYTES = 32


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Return an encoded PBKDF2 hash for ``password``.

    The salt is random per call, so two identical passwords hash to distinct
    encodings. Raises ``ValueError`` on an empty password so we never persist a
    credential that would let anyone authenticate.
    """
    if not password:
        raise ValueError("Password must not be empty.")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=_DERIVED_KEY_BYTES
    )
    return f"{_ALGORITHM}${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a stored ``encoded`` hash.

    Returns ``False`` for any malformed/unknown encoding rather than raising, so
    callers can treat a corrupt record as a failed login instead of a 500.
    """
    if not password or not isinstance(encoded, str):
        return False
    parts = encoded.split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations_raw, salt_hex, derived_hex = parts
    if algorithm != _ALGORITHM:
        return False
    try:
        iterations = int(iterations_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(derived_hex)
    except ValueError:
        return False
    if iterations <= 0:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(candidate, expected)
