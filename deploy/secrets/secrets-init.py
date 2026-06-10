from __future__ import annotations

import os
import secrets
from pathlib import Path

# secrets-init runs as root, but the gateway/bootstrap containers run as a
# non-root user (uid 10001, see Dockerfile). The secret files must therefore be
# group/other readable (0644) or the app gets `[Errno 13] Permission denied`
# when it loads ADMIN_SESSION_SECRET_FILE / EMBEDDING_SECRET_FILE. These are
# ephemeral local-dev secrets, regenerated when missing — do not reuse the mode
# or values in production.
_SECRET_MODE = 0o644


def _ensure_secret(path: Path, value: str) -> None:
    if not (path.exists() and path.read_text(encoding="utf-8").strip()):
        path.write_text(value, encoding="utf-8")
    # Always normalize perms so files created by older runs (0600) self-heal.
    os.chmod(path, _SECRET_MODE)


def main() -> None:
    root = Path("/gateway-secrets")
    root.mkdir(parents=True, exist_ok=True)
    # The non-root app user must be able to traverse the directory to read files.
    os.chmod(root, 0o755)

    _ensure_secret(root / "embedding_secret", secrets.token_urlsafe(48))
    _ensure_secret(root / "admin_session_secret", secrets.token_urlsafe(48))

    print("Initialized gateway secret files in /gateway-secrets")


if __name__ == "__main__":
    main()
