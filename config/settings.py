import base64
import binascii
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bundled, repo-published dev signing key path. Used as the offline default but
# rejected in production by the prod-safety validator below.
_DEV_DOWNSTREAM_JWT_PRIVATE_KEY_FILE = "config/dev-private-key.pem"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "mdb-mcp-gateway"
    app_version: str = "0.2.0"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000

    auth_mode: Literal["disabled", "hs256", "jwks"] = "disabled"
    mongodb_uri: str = Field(default="mongodb://mongodb:27017/?directConnection=true")
    mongodb_uri_file: str | None = None
    mongodb_db_name: str = "mcp_gateway"
    qe_enabled: bool = False
    kms_provider: Literal["none", "local", "aws"] = "none"
    qe_key_vault_namespace: str = "encryption.__keyVault"
    crypt_shared_lib_path: str | None = None
    qe_local_master_key: str = ""
    qe_local_master_key_file: str | None = None
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_secret_access_key_file: str | None = None
    aws_default_region: str = "us-east-1"
    aws_kms_endpoint: str | None = None
    aws_kms_key_arn: str = ""
    aws_kms_key_arn_file: str | None = None

    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "nomic-embed-text"
    # Fallback/declared width for the Ollama default provider. With multi-provider
    # support the active width is detected at runtime (by embedding a probe string),
    # so this is only used as the Ollama default and as a last-resort fallback.
    ollama_dimensions: int = 768

    # Embedding provider selection. These env values are the boot-time default;
    # the admin panel can persist an override in the control DB that takes
    # precedence over them.
    embedding_provider: Literal["ollama", "openai", "azure_openai", "voyage", "gemini"] = "ollama"
    # When unset, each provider falls back to a sensible default model.
    embedding_model: str | None = None
    embedding_base_url: str | None = None
    embedding_api_key: str = ""
    embedding_api_key_file: str | None = None
    # Voyage AI is MongoDB's first-party embedding/rerank stack, so it gets a
    # dedicated, drop-in env var: setting VOYAGE_API_KEY alone is enough to both
    # select AND authenticate Voyage. It promotes the *default* provider (ollama)
    # to voyage and supplies the key when no generic EMBEDDING_API_KEY is set. An
    # explicit EMBEDDING_PROVIDER / EMBEDDING_API_KEY always wins, and the admin
    # panel can still override everything at runtime.
    voyage_api_key: str = ""
    voyage_api_key_file: str | None = None
    azure_openai_endpoint: str | None = None
    azure_openai_api_version: str = "2023-05-15"
    azure_openai_deployment: str | None = None
    # Short text embedded once to measure a provider's native vector width at
    # config-apply/startup time, so `dimensions` never has to be hand-configured.
    embedding_probe_text: str = "embedding dimension probe"
    # Symmetric secret used to encrypt provider API keys at rest in the control DB.
    # Falls back to admin_session_secret / jwt_secret when unset.
    embedding_secret: str | None = None
    embedding_secret_file: str | None = None

    jwt_secret: str = "dev-secret"
    jwt_secret_file: str | None = None
    jwt_algorithm: str = "HS256"
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwks_uri: str | None = None
    jwks_uri_file: str | None = None
    jwks_local_path: str | None = None
    jwks_cache_ttl_seconds: int = 300
    # On a token whose `kid` is absent from the cached JWKS, refresh out-of-band
    # instead of waiting for the cache TTL — but no more than once per interval so
    # a flood of bogus `kid`s cannot hammer the IdP.
    jwks_min_refresh_seconds: int = 60
    # Inbound auth for the gateway's own ("virtual") MCP surface (/rpc, /mcp).
    # mcp_basic_auth_enabled lets MCP clients send HTTP Basic (username/password)
    # directly on the MCP surface; otherwise clients exchange credentials for a
    # bearer at POST /auth/token. oauth_metadata_enabled advertises RFC 9728
    # OAuth 2.0 Protected Resource Metadata so spec-compliant MCP clients can
    # discover the configured IdP; it is auto-on under auth_mode=jwks.
    mcp_basic_auth_enabled: bool = False
    oauth_metadata_enabled: bool = False
    admin_ui_enabled: bool = True
    admin_ui_path: str = "/ui"
    admin_email: str | None = None
    admin_password: str = ""
    admin_password_file: str | None = None
    admin_session_secret: str | None = None
    admin_session_secret_file: str | None = None
    admin_session_ttl_seconds: int = 28800
    default_tenant_id: str = "local-dev"
    tenant_db_prefix: str = "tenant_"
    gateway_instance_id: str | None = None
    watcher_resume_ttl_seconds: int = 86400
    platform_admin_role: str = "platform-admin"
    # Feature flag for transport="code" execution. When false, code tools remain
    # authorable/searchable but tools/call returns a clear "disabled" error.
    # Keep enabled only when a sandbox runtime (code_executor != "disabled") is available.
    code_tool_execution_enabled: bool = False
    # Pending approval actions expire automatically after this TTL.
    confirmation_ttl_seconds: int = 3600
    # Tenant usage quota period and default limits (0 => unlimited).
    usage_quota_period: Literal["monthly"] = "monthly"
    default_quota_calls_per_period: int = 0
    default_quota_sandbox_seconds_per_period: int = 0
    # Reject a code-tool call up front when its worst-case sandbox cost cannot fit
    # the tenant's remaining sandbox-seconds budget, instead of starting work that
    # gets killed mid-flight. Only applies when a sandbox-seconds quota is set.
    quota_preflight_enabled: bool = True
    # How long a tenant's suspended/active status is cached per replica. Keeps the
    # hot-path status check cheap; also the max delay before a suspend issued on
    # one replica is honored by the others. 0 => no cache (read every request).
    tenant_status_cache_ttl_seconds: int = 5
    # Soft-delete retention: a deleted tenant is locked out immediately but its
    # database is kept for this many days so the delete can be reversed, then the
    # purge reaper drops it permanently.
    tenant_retention_days: int = 30
    # Cadence of the background reaper that hard-drops soft-deleted tenants whose
    # retention window has elapsed. 0 => disabled (no reaper runs; purge is then a
    # manual/explicit hard-delete only).
    tenant_purge_sweep_interval_seconds: int = 0
    # Runtime for executing transport="code" tools when execution is enabled.
    # "wasm" runs code in the WebAssembly sandbox worker; "disabled" keeps the
    # runtime hard-off regardless of code_tool_execution_enabled.
    code_executor: Literal["wasm", "disabled"] = "wasm"
    # Pinned CPython-on-WASI binary fetched by `make fetch-wasm`.
    sandbox_python_wasm_path: str = "vendor/python-3.12.0.wasm"
    # Per-call sandbox resource controls.
    sandbox_fuel: int = 4_000_000_000
    sandbox_memory_bytes: int = 268_435_456  # 256 MiB
    # 0 => inherit downstream_timeout_ms
    sandbox_wall_timeout_ms: int = 0
    sandbox_max_output_bytes: int = 262_144
    sandbox_max_concurrency_per_tenant: int = 4
    # Process-wide ceiling on simultaneous sandbox executions across ALL tenants.
    # Bounds total host load so N tenants each at their per-tenant limit cannot
    # collectively spawn an unbounded number of concurrent workers. 0 => no
    # global cap (only the per-tenant limit applies).
    sandbox_max_global_concurrency: int = 0
    # Code-tool requirements are installed with host `pip` BEFORE the wasm jail,
    # so a source build could run setup.py on the host. They are therefore
    # DENY-by-default: only distribution names listed here (comma/space separated,
    # e.g. "httpx,orjson") may be installed, and only as prebuilt wheels (no source
    # builds). Empty => code tools may use the in-sandbox stdlib only.
    sandbox_allowed_requirements: str = ""
    # Warm sandbox worker pool. 0 => disabled (a throwaway worker subprocess is
    # spawned per call, paying the wasm-compile cold start each time). >0 keeps
    # that many CPython-on-WASI workers resident and prewarmed so calls reuse an
    # already-compiled runtime; every job still runs in a fresh wasm Store, so
    # isolation stays per-call.
    sandbox_pool_size: int = 0
    # Recycle a pooled worker after this many jobs (0 => never) as a leak/RSS
    # backstop for long-lived workers.
    sandbox_worker_max_jobs: int = 0
    # Proactively retire an idle pooled worker once it has been resident this long
    # (seconds), complementing the reactive per-job recycle above. Bounds memory
    # fragmentation / drift on long-lived workers. 0 => never retire by age.
    sandbox_worker_max_age_seconds: int = 0
    # Cadence of the background pool sweep that retires over-age idle workers and
    # discards idle workers that fail a health ping. 0 => disabled (no sweep).
    sandbox_pool_sweep_interval_seconds: int = 0
    # Max wait for a free pooled worker before failing a call (0 => derive from
    # the sandbox wall timeout).
    sandbox_pool_acquire_timeout_ms: int = 0
    # Max wait for a freshly spawned worker to compile its module and report ready.
    sandbox_pool_warmup_timeout_ms: int = 20_000
    # Extra host-side wait, ON TOP of a job's wall-clock budget, for a throwaway
    # worker to spawn and instantiate the wasm runtime (the cold start) before a
    # result is expected. The guest self-enforces its wall/CPU deadline internally
    # (wasm epoch + fuel + a CPU rlimit), so the host deadline is only a backstop
    # for a hung/dead worker; this grace stops a slow *cold start* under host load
    # from being misread as a guest timeout. A dead worker is still detected
    # immediately (its stdout closes), so the grace only ever helps a live, slowly
    # warming worker. Also acts as a floor for the pool's acquire wait so a
    # mid-job worker death + respawn under load doesn't starve the next call.
    sandbox_worker_startup_grace_ms: int = 10_000
    # Directory for the serialized, compiled python.wasm module so respawned
    # workers warm up via deserialize instead of recompiling. Empty => disabled.
    sandbox_module_cache_path: str = "vendor/.wasm-cache"
    # Enable the tenant-scoped virtual database bridge (`context.db`) for code
    # tools. The sandbox remains network-isolated; DB access is relayed through
    # the host process and scoped to the caller tenant.
    sandbox_db_bridge_enabled: bool = False
    # Hard bounds for host-side DB RPCs initiated by a single code-tool run.
    sandbox_db_max_docs: int = 100
    sandbox_db_query_timeout_ms: int = 1000
    sandbox_db_max_calls_per_invocation: int = 25
    sandbox_db_max_result_bytes: int = 131072
    # Enable the tenant-scoped cross-tool call bridge (`context.tools` /
    # `context.call`) so a code tool can invoke sibling code tools in the same
    # tenant namespace. Calls are relayed through the host, re-authorized against
    # the original caller's scopes, restricted to code tools, refuse
    # confirmation-gated tools (no human in the loop), and are bounded by a
    # nesting depth + per-invocation call budget. The sandbox stays
    # network-isolated; only sibling code tools become reachable.
    sandbox_tool_bridge_enabled: bool = False
    # Max sibling tool calls a single code-tool invocation may issue.
    sandbox_tool_max_calls_per_invocation: int = 10
    # Max nesting depth for cross-tool calls (A->B->C). Bounds recursion + cycles
    # so a tool that calls itself (directly or transitively) fails closed.
    sandbox_tool_call_max_depth: int = 3
    # Per-result size ceiling for a relayed cross-tool call response.
    sandbox_tool_max_result_bytes: int = 262_144
    # When a request arrives for a tenant that has never been provisioned, create
    # its database + indexes on first use. Disable in environments where tenant
    # ids come from untrusted callers and provisioning must be an explicit step.
    auto_provision_tenants: bool = True
    auto_bootstrap: bool = False

    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 120

    hybrid_vector_weight: float = 0.5
    hybrid_text_weight: float = 0.5
    hybrid_num_candidates: int = 100
    hybrid_pipeline_limit: int = 20
    hybrid_output_limit: int = 10
    include_score_details: bool = True
    fusion_strategy: Literal["rank_fusion", "score_fusion", "app_side"] = "rank_fusion"
    # Pin tools flagged ``metadata.always_included`` to the top of every routed
    # result regardless of relevance (still scope-filtered). Ops escape hatch.
    hybrid_pin_always_included: bool = True

    # Semantic tools/list discovery: when an X-MCP-Query header (or params.query)
    # is present, tools/list returns a curated shortlist instead of the full catalog.
    route_top_k: int = 5
    catalog_list_limit: int = 200
    query_header: str = "x-mcp-query"
    scopes_header: str = "x-mcp-scopes"

    semantic_cache_threshold: float = 0.95
    http_timeout_seconds: int = 20
    embedding_timeout_seconds: float = 8.0
    embedding_retry_attempts: int = 2
    embedding_retry_backoff_seconds: float = 0.25
    embedding_circuit_failures: int = 5
    embedding_circuit_reset_seconds: int = 30
    embedding_cache_ttl_seconds: int = 300
    embedding_cache_max_entries: int = 512
    # Semantic-cache / catalog re-embedding migration is streamed in bounded pages
    # so a very large cache never materializes in memory at once. fetch_page_size
    # bounds how many stale docs are pulled per round trip; embed_concurrency caps
    # simultaneous re-embed writes (mirrors the sandbox concurrency pattern).
    cache_migration_fetch_page_size: int = 500
    cache_migration_embed_concurrency: int = 8
    # Hard deadline for a single downstream JSON-RPC call (Section 4 of the blog).
    downstream_timeout_ms: int = 2000
    downstream_jwt_enabled: bool = True
    downstream_jwt_algorithm: str = "RS256"
    downstream_jwt_private_key: str = ""
    downstream_jwt_private_key_file: str | None = _DEV_DOWNSTREAM_JWT_PRIVATE_KEY_FILE
    downstream_jwt_kid: str = "dev-local-key-1"
    downstream_jwt_issuer: str = "mdb-mcp-gateway"
    downstream_token_ttl_seconds: int = 120
    downstream_token_refresh_skew_seconds: int = 15
    downstream_auth_header: str = "Authorization"
    downstream_token_env_var: str = "MCP_DOWNSTREAM_TOKEN"
    downstream_allow_insecure_credentials: bool = False

    enable_metrics: bool = True
    enable_tracing: bool = False
    log_json: bool = True
    request_max_bytes: int = 262144
    tenant_endpoint_ssrf_guard: bool = True
    # Outbound downstream egress allowlisting. Global allowlist is a comma/space
    # separated set of host globs / exact hosts / CIDRs (e.g.
    # "*.corp.example,api.vendor.com,203.0.113.0/24"). Enforcement remains
    # no-op-compatible until explicitly configured (see services/egress_policy.py).
    egress_allowlist_enabled: bool = True
    egress_global_allowlist: str = ""
    egress_default_deny: bool = False
    egress_allowlist_cache_ttl_seconds: int = 5
    guardrail_ml_enabled: bool = False
    guardrail_injection_threshold: float = 0.85
    guardrail_fail_mode: Literal["open", "closed"] = "open"
    guardrail_timeout_seconds: float = 1.5
    guardrail_circuit_failures: int = 5
    guardrail_circuit_reset_seconds: int = 30
    guardrail_signature_top_k: int = 3
    guardrail_pii_ner_enabled: bool = False
    cors_allow_origins: str = "*"

    atlas_tls: bool = False
    atlas_tls_ca_file: str | None = None
    atlas_auth_source: str | None = None
    atlas_auth_mechanism: str | None = None
    atlas_username: str | None = None
    atlas_password: str | None = None
    atlas_password_file: str | None = None

    @model_validator(mode="after")
    def _load_file_backed_values(self) -> "Settings":
        file_backed_fields = [
            ("mongodb_uri_file", "mongodb_uri"),
            ("jwt_secret_file", "jwt_secret"),
            ("jwks_uri_file", "jwks_uri"),
            ("atlas_password_file", "atlas_password"),
            ("admin_password_file", "admin_password"),
            ("admin_session_secret_file", "admin_session_secret"),
            ("embedding_api_key_file", "embedding_api_key"),
            ("voyage_api_key_file", "voyage_api_key"),
            ("embedding_secret_file", "embedding_secret"),
            ("downstream_jwt_private_key_file", "downstream_jwt_private_key"),
            ("qe_local_master_key_file", "qe_local_master_key"),
            ("aws_secret_access_key_file", "aws_secret_access_key"),
            ("aws_kms_key_arn_file", "aws_kms_key_arn"),
        ]
        for file_field, value_field in file_backed_fields:
            file_path = getattr(self, file_field)
            if not file_path:
                continue
            text = Path(file_path).read_text(encoding="utf-8").strip()
            if text:
                setattr(self, value_field, text)
        return self

    @model_validator(mode="after")
    def _apply_admin_defaults(self) -> "Settings":
        path = self.admin_ui_path.strip()
        if not path:
            path = "/ui"
        if not path.startswith("/"):
            path = f"/{path}"
        normalized = path.rstrip("/")
        self.admin_ui_path = normalized or "/"
        if not self.admin_session_secret:
            self.admin_session_secret = self.jwt_secret
        if not self.embedding_secret:
            self.embedding_secret = self.admin_session_secret or self.jwt_secret
        if self.sandbox_wall_timeout_ms <= 0:
            self.sandbox_wall_timeout_ms = max(1, self.downstream_timeout_ms)
        return self

    @model_validator(mode="after")
    def _validate_prod_safety(self) -> "Settings":
        env = self.environment.lower()
        is_prod = env in {"prod", "production"}
        if not is_prod:
            return self
        if self.auth_mode == "disabled":
            raise ValueError("auth_mode=disabled is not allowed in production.")
        if self.auth_mode == "hs256":
            weak = {"dev-secret", "change-me", "secret", "password"}
            if len(self.jwt_secret) < 16 or self.jwt_secret in weak:
                raise ValueError("HS256 jwt_secret is too weak for production.")
        if self.auth_mode == "jwks":
            if not self.jwt_issuer or not self.jwt_audience:
                raise ValueError("jwt_issuer and jwt_audience are required for JWKS auth.")
            if not self.jwks_uri and not self.jwks_local_path:
                raise ValueError("Provide jwks_uri or jwks_local_path for JWKS auth.")
        # A wildcard CORS policy lets any origin's browser drive the gateway with a
        # caller's credentials. Fail closed at boot rather than silently shipping it.
        if self.cors_allow_origins.strip() == "*":
            raise ValueError("Wildcard cors_allow_origins is not allowed in production.")
        if self.admin_ui_enabled:
            weak = {"dev-secret", "change-me", "secret", "password", "demo"}
            if not self.admin_email:
                raise ValueError("admin_email is required when admin_ui_enabled in production.")
            if len(self.admin_password) < 12 or self.admin_password.lower() in weak:
                raise ValueError("admin_password is too weak for production.")
            session_secret = self.admin_session_secret or ""
            if len(session_secret) < 16 or session_secret in weak:
                raise ValueError("admin_session_secret is too weak for production.")
        # The bundled dev keypair is published in this repo; signing downstream
        # workload tokens with it in production would let anyone forge them.
        if (
            self.downstream_jwt_enabled
            and self.downstream_jwt_private_key_file == _DEV_DOWNSTREAM_JWT_PRIVATE_KEY_FILE
        ):
            raise ValueError(
                "Configure a production DOWNSTREAM_JWT_PRIVATE_KEY(_FILE); the bundled "
                "dev signing key must not be used in production."
            )
        if self.qe_enabled:
            if self.kms_provider == "none":
                raise ValueError("Set KMS_PROVIDER=local or KMS_PROVIDER=aws when QE is enabled.")
            if self.kms_provider == "local":
                if not self.qe_local_master_key:
                    raise ValueError(
                        "QE local mode requires QE_LOCAL_MASTER_KEY or QE_LOCAL_MASTER_KEY_FILE."
                    )
                try:
                    decoded = base64.b64decode(self.qe_local_master_key, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("QE_LOCAL_MASTER_KEY must be valid base64.") from exc
                if len(decoded) != 96:
                    raise ValueError("QE_LOCAL_MASTER_KEY must decode to exactly 96 bytes.")
            if self.kms_provider == "aws" and not self.aws_kms_key_arn:
                raise ValueError("AWS KMS mode requires AWS_KMS_KEY_ARN or AWS_KMS_KEY_ARN_FILE.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
