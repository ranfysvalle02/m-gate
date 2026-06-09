from __future__ import annotations

import os
import secrets
from pathlib import Path


def _write_if_missing(path: Path, value: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> None:
    root = Path("/gateway-secrets")
    root.mkdir(parents=True, exist_ok=True)

    _write_if_missing(root / "embedding_secret", secrets.token_urlsafe(48))
    _write_if_missing(root / "admin_session_secret", secrets.token_urlsafe(48))

    print("Initialized gateway secret files in /gateway-secrets")


if __name__ == "__main__":
    main()
