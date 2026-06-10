#!/usr/bin/env python3
"""Download and verify the pinned CPython-on-WASI runtime artifact."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from urllib.request import urlopen

DEFAULT_URL = (
    "https://github.com/vmware-labs/webassembly-language-runtimes/releases/download/"
    "python%2F3.12.0%2B20231211-040d5a6/python-3.12.0.wasm"
)
DEFAULT_SHA256 = "e5dc5a398b07b54ea8fdb503bf68fb583d533f10ec3f930963e02b9505f7a763"
DEFAULT_OUTPUT = Path("vendor/python-3.12.0.wasm")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _download(url: str) -> bytes:
    with urlopen(url, timeout=120) as response:  # noqa: S310 - pinned URL only
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.getenv("PYTHON_WASM_URL", DEFAULT_URL),
        help="Pinned artifact URL",
    )
    parser.add_argument(
        "--sha256",
        default=os.getenv("PYTHON_WASM_SHA256", DEFAULT_SHA256),
        help="Expected SHA256 digest",
    )
    parser.add_argument(
        "--output",
        default=os.getenv("PYTHON_WASM_OUTPUT", str(DEFAULT_OUTPUT)),
        help="Destination path",
    )
    args = parser.parse_args()

    expected = args.sha256.strip().lower()
    if len(expected) != 64:
        raise SystemExit("Expected a 64-char SHA256 digest for --sha256.")

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _download(args.url)
    digest = _sha256_bytes(payload)
    if digest != expected:
        raise SystemExit(
            f"Checksum mismatch for downloaded python.wasm: expected {expected}, got {digest}."
        )
    destination.write_bytes(payload)
    print(f"Wrote verified python.wasm to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
