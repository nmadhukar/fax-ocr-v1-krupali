"""
Application settings using Pydantic v2 Settings.

All configuration is loaded from environment variables with sensible defaults.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    model_config = SettingsConfigDict(env_prefix="")

    database_url: str = Field(
        default="",
        alias="DATABASE_URL",
    )
    pool_size: int = Field(default=10, alias="DB_POOL_SIZE")
    max_overflow: int = Field(default=20, alias="DB_MAX_OVERFLOW")
    pool_timeout: int = Field(default=30, alias="DB_POOL_TIMEOUT")
    echo_sql: bool = Field(default=False, alias="DB_ECHO_SQL")


class RedisSettings(BaseSettings):
    """Redis and Celery settings."""

    model_config = SettingsConfigDict(env_prefix="")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    celery_broker_url: str = Field(
        default="redis://localhost:6379/0", alias="CELERY_BROKER_URL"
    )
    celery_result_backend: str = Field(
        default="redis://localhost:6379/1", alias="CELERY_RESULT_BACKEND"
    )


class MinioSettings(BaseSettings):
    """MinIO/S3 storage settings."""

    model_config = SettingsConfigDict(env_prefix="MINIO_")

    endpoint: str = Field(default="localhost:9000")
    # External endpoint used for presigned URLs returned to browsers.
    # In Docker, endpoint=minio:9000 (internal) but external_endpoint=localhost:9000
    external_endpoint: str | None = Field(default=None, alias="MINIO_EXTERNAL_ENDPOINT")
    access_key: str = Field(default="")
    secret_key: SecretStr = Field(default="")
    secure: bool = Field(default=False)
    bucket_name: str = Field(default="fax-documents")
    templates_bucket: str = Field(default="templates")


class OcrSettings(BaseSettings):
    """OCR processing settings."""

    model_config = SettingsConfigDict(env_prefix="OCR_")

    enable_gpu: bool = Field(default=False)
    dpi_target: int = Field(default=300)
    lang: str = Field(default="en")
    use_angle_cls: bool = Field(default=True)
    det_db_thresh: float = Field(default=0.3)
    det_db_box_thresh: float = Field(default=0.5)
    rec_batch_num: int = Field(default=6)
    max_batch_size: int = Field(default=10)


class TemplateMatchingSettings(BaseSettings):
    """Template matching thresholds."""

    model_config = SettingsConfigDict(env_prefix="TEMPLATE_")

    phash_threshold: int = Field(default=5)
    orb_min_matches: int = Field(default=20)
    match_min_score: float = Field(default=0.75)
    orb_n_features: int = Field(default=500)
    flann_table_number: int = Field(default=6)
    flann_key_size: int = Field(default=12)
    flann_multi_probe_level: int = Field(default=1)
    lowe_ratio: float = Field(default=0.7)


class VlmSettings(BaseSettings):
    """VLM settings for LayoutLM Document QA extraction."""

    model_config = SettingsConfigDict(
        env_prefix="VLM_",
        protected_namespaces=("settings_",),
    )

    use_gpu: bool = Field(
        default=False,
        description="Use GPU for LayoutLM inference (requires CUDA)",
    )

    # LayoutLM Document QA
    layoutlm_model_name: str = Field(
        default="impira/layoutlm-document-qa",
        description="LayoutLM model name (HuggingFace model ID)",
    )
    layoutlm_adapter_path: str | None = Field(
        default=None,
        description="Path to LoRA adapter for fine-tuned LayoutLM model",
    )
    layoutlm_enabled: bool = Field(
        default=True,
        description="Enable LayoutLM Document QA extraction",
    )
    layoutlm_score_multiplier: float = Field(
        default=0.70,
        description=(
            "LayoutLM score multiplier relative to template_bonus. "
            "0.70 for base model. 1.20 after fine-tuning. "
            "Env var: VLM_LAYOUTLM_SCORE_MULTIPLIER"
        ),
    )
    max_pages: int = Field(
        default=5,
        description="Maximum pages to process per job for LayoutLM extraction.",
    )


class ConfidenceSettings(BaseSettings):
    """Confidence thresholds for auto-finalization."""

    model_config = SettingsConfigDict(env_prefix="CONFIDENCE_")

    auto_finalize: float = Field(default=0.70)
    field_min: float = Field(default=0.50)
    critical_field_min: float = Field(default=0.65)


class EmbeddingSettings(BaseSettings):
    """Embedding model settings (sentence-transformers, local)."""

    model_config = SettingsConfigDict(
        env_prefix="EMBEDDER_",
        protected_namespaces=("settings_",),
    )

    model_name: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        description="HuggingFace model ID for sentence embeddings",
    )


class FeatureFlags(BaseSettings):
    """Feature flags for phase rollout."""

    model_config = SettingsConfigDict(env_prefix="ENABLE_")

    vlm: bool = Field(default=True)
    vector_search: bool = Field(default=True)
    feedback_loop: bool = Field(default=True)


class HitlSettings(BaseSettings):
    """Human-in-the-Loop (HITL) review layer settings."""

    model_config = SettingsConfigDict(env_prefix="HITL_")

    enabled: bool = Field(
        default=True,
        description="Enable HITL field-level flagging for low-confidence extractions",
    )
    default_threshold: float = Field(
        default=0.75,
        description=(
            "Confidence threshold below which non-critical fields are flagged for review. "
            "Env var: HITL_DEFAULT_THRESHOLD"
        ),
    )
    critical_threshold: float = Field(
        default=0.85,
        description=(
            "Confidence threshold below which critical clinical fields "
            "(patient_name, member_id, prior_auth_number, auth dates, decision, "
            "service_code, diagnosis_code) are flagged for review. "
            "Env var: HITL_CRITICAL_THRESHOLD"
        ),
    )
    min_flags_for_review: int = Field(
        default=1,
        description=(
            "Minimum number of flagged fields required to trigger NEEDS_REVIEW status. "
            "Set to 0 to disable HITL-driven routing (flags computed but don't affect status). "
            "Env var: HITL_MIN_FLAGS_FOR_REVIEW"
        ),
    )


class AdaptiveExtractionSettings(BaseSettings):
    """Adaptive extraction/routing/ranker settings."""

    model_config = SettingsConfigDict(env_prefix="ADAPTIVE_")

    section_routing_enabled: bool = Field(
        default=True,
        description="Enable page section routing before extraction",
    )
    hard_field_adjudication_enabled: bool = Field(
        default=True,
        description="Enable conflict adjudication for low-confidence hard fields",
    )
    hard_field_threshold: float = Field(
        default=0.72,
        description="Adjudication trigger threshold for best candidate confidence",
    )
    hard_field_conflict_gap: float = Field(
        default=0.12,
        description="Conflict trigger: top two candidates within this confidence gap",
    )
    candidate_ranker_enabled: bool = Field(
        default=True,
        description="Enable learned candidate ranker overlay",
    )
    candidate_ranker_model_path: str = Field(
        default="models/candidate_ranker/model.json",
        description="Path to learned candidate ranker model JSON",
    )
    template_drift_enabled: bool = Field(
        default=True,
        description="Enable template drift detection metadata",
    )
    template_drift_low_match_threshold: float = Field(
        default=0.72,
        description="Template match score below this indicates potential drift",
    )
    template_drift_missing_critical_threshold: float = Field(
        default=0.35,
        description="Missing critical field ratio threshold for drift flag",
    )


class ApiSettings(BaseSettings):
    """API server settings."""

    # Disable automatic JSON decoding so comma-separated env vars like
    # API_ALLOWED_ORIGINS work in Docker/.env files without JSON syntax.
    model_config = SettingsConfigDict(env_prefix="API_", enable_decoding=False)

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    debug: bool = Field(default=False)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    allowed_origins: list[str] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:5173",
            "http://localhost:8000",
            "http://localhost:8001",
            "http://localhost:8002",
            "http://localhost:8003",
            "http://host.docker.internal:3000",
            "http://host.docker.internal:5173",
            "http://host.docker.internal:8000",
            "http://host.docker.internal:8001",
            "http://host.docker.internal:8002",
            "http://host.docker.internal:8003",
        ]
    )
    allowed_hosts: list[str] = Field(
        default=["*"],
        description=(
            "Allowed hostnames for TrustedHostMiddleware. "
            "Set to specific domains in production (e.g. 'api.example.com'). "
            "Use '*' (default) to allow any host in development. "
            "Env var: API_ALLOWED_HOSTS (comma-separated)"
        ),
    )
    max_upload_size_mb: int = Field(default=50)

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated origins string."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def parse_hosts(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated allowed hosts string."""
        if isinstance(v, str):
            return [h.strip() for h in v.split(",")]
        return v


class SecuritySettings(BaseSettings):
    """Security-related settings."""

    model_config = SettingsConfigDict(env_prefix="")

    secret_key: SecretStr = Field(
        default="", alias="SECRET_KEY"
    )
    encryption_key: SecretStr | None = Field(default=None, alias="ENCRYPTION_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=30, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )


class Settings(BaseSettings):
    """
    Main settings aggregator.

    Combines all setting groups into a single configuration object.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        enable_decoding=False,
    )

    # Sub-settings
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    ocr: OcrSettings = Field(default_factory=OcrSettings)
    template: TemplateMatchingSettings = Field(default_factory=TemplateMatchingSettings)
    vlm: VlmSettings = Field(default_factory=VlmSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    confidence: ConfidenceSettings = Field(default_factory=ConfidenceSettings)
    hitl: HitlSettings = Field(default_factory=HitlSettings)
    adaptive: AdaptiveExtractionSettings = Field(default_factory=AdaptiveExtractionSettings)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    api: ApiSettings = Field(default_factory=ApiSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    # Application metadata
    app_name: str = Field(default="Healthcare Fax Processor")
    app_version: str = Field(default="1.0.0")
    environment: Literal["development", "staging", "production"] = Field(
        default="development", alias="ENVIRONMENT"
    )


_INSECURE_DEFAULTS = {
    "your-secret-key-change-in-production",
    "changeme",
    "secret",
    "password",
    "test",
    "admin",
    "default",
    "12345678",
}


@lru_cache
def get_settings() -> Settings:
    """
    Get cached application settings.

    Uses LRU cache to ensure settings are only loaded once.
    In production, validates that insecure default secrets are not used.

    Returns:
        Settings instance with all configuration loaded.

    Raises:
        RuntimeError: If insecure defaults are detected in production.
    """
    import logging
    _logger = logging.getLogger(__name__)

    settings = Settings()

    if settings.environment == "production":
        # Enforce that default secrets are overridden
        secret_val = settings.security.secret_key.get_secret_value()
        if secret_val in _INSECURE_DEFAULTS:
            raise RuntimeError(
                "FATAL: SECRET_KEY is still set to a default value. "
                "Set the SECRET_KEY environment variable to a secure random value."
            )
        if len(secret_val) < 32:
            raise RuntimeError(
                "FATAL: SECRET_KEY must be at least 32 characters in production. "
                "Use: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        minio_secret = settings.minio.secret_key.get_secret_value()
        if minio_secret in ("minioadmin123", "minioadmin"):
            raise RuntimeError(
                "FATAL: MINIO_SECRET_KEY is still set to the default value. "
                "Set MINIO_SECRET_KEY to a secure value before deploying to production."
            )
        if "faxpass123" in settings.database.database_url:
            raise RuntimeError(
                "FATAL: DATABASE_URL contains default credentials (faxpass123). "
                "Change the database password before deploying to production."
            )
    elif settings.environment == "staging":
        secret_val = settings.security.secret_key.get_secret_value()
        if secret_val in _INSECURE_DEFAULTS:
            _logger.warning(
                "SECRET_KEY is set to a default value on staging. "
                "Consider using a unique secret for staging environments."
            )
        minio_secret = settings.minio.secret_key.get_secret_value()
        if not minio_secret or minio_secret in ("minioadmin123", "minioadmin"):
            _logger.warning(
                "MINIO_SECRET_KEY is empty or set to a default value on staging. "
                "Set MINIO_SECRET_KEY to a secure value."
            )
        if not settings.database.database_url or "faxpass123" in settings.database.database_url:
            _logger.warning(
                "DATABASE_URL is empty or contains default credentials on staging. "
                "Change the database password for staging environments."
            )

    return settings
