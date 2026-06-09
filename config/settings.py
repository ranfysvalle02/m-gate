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
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000

    auth_mode: Literal["disabled", "hs256", "jwks"] = "disabled"
    mongodb_uri: str = Field(default="mongodb://mongodb:27017/?directConnection=true")
    mongodb_uri_file: str | None = None
    mongodb_db_name: str = "mcp_gateway"

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

    enable_metrics: bool = True
    enable_tracing: bool = False
    log_json: bool = True
    request_max_bytes: int = 262144
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
            ("embedding_secret_file", "embedding_secret"),
            ("downstream_jwt_private_key_file", "downstream_jwt_private_key"),
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
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
