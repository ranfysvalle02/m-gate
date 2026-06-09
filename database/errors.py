from __future__ import annotations

from typing import Any

from pymongo.errors import OperationFailure

_INDEX_ALREADY_EXISTS_CODES = {68}
_NAMESPACE_NOT_FOUND_CODES = {26}
_INDEX_NOT_FOUND_CODES = {27}


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
    # Atlas vector indexes can briefly report NOT_STARTED while materializing.
    return "IndexNotFound" in message or "state NOT_STARTED" in message
