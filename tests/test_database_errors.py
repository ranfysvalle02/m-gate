from pymongo.errors import OperationFailure

from database.errors import (
    is_index_already_exists,
    is_index_not_queryable_yet,
    is_namespace_not_found,
)


def test_is_index_already_exists_prefers_structured_codes():
    exc = OperationFailure(
        "index collision",
        code=68,
        details={"codeName": "IndexAlreadyExists"},
    )
    assert is_index_already_exists(exc) is True


def test_is_namespace_not_found_prefers_structured_codes():
    exc = OperationFailure(
        "missing namespace",
        code=26,
        details={"codeName": "NamespaceNotFound"},
    )
    assert is_namespace_not_found(exc) is True


def test_is_index_not_queryable_yet_handles_not_started_fallback():
    exc = OperationFailure("index in state NOT_STARTED")
    assert is_index_not_queryable_yet(exc) is True


def test_is_index_not_queryable_yet_handles_unknown_state_vector_index():
    # Real Atlas Local failure: a freshly-created vector index queried before it
    # is materialized reports state UNKNOWN via code 8 / UnknownError.
    exc = OperationFailure(
        "PlanExecutor error during aggregation :: caused by :: cannot query vector "
        "index 6a28a1dae233546bf156a2b1 (vector index semantic-cache-v-nomic-embed-text-768 "
        "collection semantic_cache (90714fa3) in database tenant_itest_a6be5618) "
        "while in state UNKNOWN",
        code=8,
        details={"codeName": "UnknownError"},
    )
    assert is_index_not_queryable_yet(exc) is True


def test_is_index_not_queryable_yet_does_not_mask_failed_state():
    # A terminal FAILED state is a real error and must propagate, not be retried.
    exc = OperationFailure(
        "cannot query vector index abc while in state FAILED",
        code=8,
        details={"codeName": "UnknownError"},
    )
    assert is_index_not_queryable_yet(exc) is False


def test_is_index_already_exists_substring_fallback_without_metadata():
    # Older servers may omit code/codeName; the lower-cased message is the floor.
    exc = OperationFailure("Index already exists with a different name")
    assert is_index_already_exists(exc) is True


def test_is_index_already_exists_reads_code_name_attribute():
    exc = OperationFailure("collision")
    # Simulate a driver that exposes code_name but no details dict.
    exc.code_name = "IndexAlreadyExists"  # type: ignore[attr-defined]
    assert is_index_already_exists(exc) is True


def test_is_namespace_not_found_substring_fallback():
    exc = OperationFailure("ns not found: namespace not found here")
    assert is_namespace_not_found(exc) is True


def test_is_index_not_queryable_yet_via_index_not_found_code():
    exc = OperationFailure("missing", code=27, details={"codeName": "IndexNotFound"})
    assert is_index_not_queryable_yet(exc) is True


def test_predicates_return_false_for_unrelated_error():
    exc = OperationFailure("some unrelated failure", code=11000)
    assert is_index_already_exists(exc) is False
    assert is_namespace_not_found(exc) is False
    assert is_index_not_queryable_yet(exc) is False
