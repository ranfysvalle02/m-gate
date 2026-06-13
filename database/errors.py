from __future__ import annotations

from typing import Any

from pymongo.errors import OperationFailure

_INDEX_ALREADY_EXISTS_CODES = {68}
_NAMESPACE_NOT_FOUND_CODES = {26}
_INDEX_NOT_FOUND_CODES = {27}

# A freshly-created Atlas vector index passes through several transient states
# while mongot materializes it. Querying it before it is queryable raises
# "cannot query vector index ... while in state <STATE>" (often surfaced as
# code 8 / UnknownError). These states mean "not ready yet, retry"; a terminal
# FAILED state is deliberately excluded so genuine failures still propagate.
_TRANSIENT_VECTOR_INDEX_STATES = (
    "NOT_STARTED",
    "INITIAL_SYNC",
    "PENDING",
    "BUILDING",
    "UNKNOWN",
)


def _details(exc: OperationFailure) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    return details if isinstance(details, dict) else {}


def _code(exc: OperationFailure) -> int | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _code_name(exc: OperationFailure) -> str | None:
    details = _details(exc)
    code_name = details.get("codeName")
    if isinstance(code_name, str) and code_name:
        return code_name
    raw = getattr(exc, "code_name", None)
    return raw if isinstance(raw, str) and raw else None


def is_index_already_exists(exc: OperationFailure) -> bool:
    code = _code(exc)
    if code in _INDEX_ALREADY_EXISTS_CODES:
        return True
    if _code_name(exc) == "IndexAlreadyExists":
        return True
    # Fallback for older server variants that omit structured error metadata.
    return "already exists" in str(exc).lower()


def is_namespace_not_found(exc: OperationFailure) -> bool:
    code = _code(exc)
    if code in _NAMESPACE_NOT_FOUND_CODES:
        return True
    if _code_name(exc) == "NamespaceNotFound":
        return True
    return "namespace not found" in str(exc).lower()


def is_index_not_queryable_yet(exc: OperationFailure) -> bool:
    code = _code(exc)
    if code in _INDEX_NOT_FOUND_CODES:
        return True
    code_name = _code_name(exc)
    if code_name == "IndexNotFound":
        return True
    message = str(exc)
    if "IndexNotFound" in message:
        return True
    lowered = message.lower()
    # Atlas Local / mongot also surface a plain "not initialized" message while
    # a freshly-created vector index is still materializing (code 8 / UnknownError).
    if "not initialized" in lowered:
        return True
    # Atlas vector indexes briefly report a transient build state (NOT_STARTED,
    # UNKNOWN, …) while materializing; querying that early is a "not ready yet"
    # signal callers should retry, not a hard failure.
    return any(f"state {state}" in message for state in _TRANSIENT_VECTOR_INDEX_STATES)
