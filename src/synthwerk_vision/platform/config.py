from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from synthwerk_vision.api.origin import OriginError, canonical_origin


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables prefixed with SYNTHWERK_VISION_."""

    model_config = SettingsConfigDict(
        env_prefix="SYNTHWERK_VISION_", env_file=".env", extra="ignore", populate_by_name=True
    )

    platform_env: Literal["local", "ci", "dev", "stg", "prd"] = Field(
        default="local",
        validation_alias="SYNTHWERK_PLATFORM_ENV",
        description="Shared platform setting (no service prefix): the deployment environment.",
    )
    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = Field(
        default="json", description="console is used only when the environment is local."
    )
    commit: str = Field(
        default="0000000",
        pattern=r"^[0-9a-f]{7,40}$",
        description="Git commit of the build, shown by /readyz. The image sets it at build time.",
    )

    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:8080"],
        description="JSON list of first-party origins allowed to call the widget routes "
        "(classification, upload). Customer origins come from identity. Each item is "
        "scheme://host[:port], http only for localhost; an invalid item stops the start.",
    )
    identity_url: str | None = Field(
        default=None,
        description="Identity base URL as vision reaches it, e.g. http://identity:8080. "
        "Asked which customer origins an app lists (GET /internal/v1/widget-origins). "
        "Unset: the widget routes answer CORS for cors_origins only.",
    )
    storage_backend: Literal["local", "s3"] = Field(
        default="local",
        description="local: files in upload_dir (development). s3: an S3-compatible bucket.",
    )
    upload_dir: Path = Path("uploads")
    upload_quota_bytes: int = Field(
        default=1024 * 1024 * 1024,
        gt=0,
        description="Bytes POST /v1/images may keep per org; more answers 507.",
    )
    upload_quota_files: int = Field(
        default=10_000,
        gt=0,
        description="Files POST /v1/images may keep per org; more answers 507.",
    )
    upload_quota_total_bytes: int = Field(
        default=5 * 1024 * 1024 * 1024,
        gt=0,
        description="Bytes POST /v1/images may keep for all orgs together; more answers 507.",
    )
    upload_writers: int = Field(
        default=2,
        ge=1,
        description="Worker processes times instances that write uploads to the same store. "
        "Near a quota, usage is scanned again before each write when it is within "
        "max_upload_bytes x upload_writers of the limit.",
    )
    s3_endpoint: str | None = Field(
        default=None,
        description="S3 API URL, e.g. https://<project>.storage.supabase.co/storage/v1/s3.",
    )
    s3_region: str | None = Field(default=None, description="Region of the bucket.")
    s3_bucket: str | None = Field(default=None, description="Private bucket for uploads.")
    s3_access_key_id: SecretStr | None = None
    s3_access_key_id_file: Path | None = Field(
        default=None, description="File that holds the access key id (instead of the value)."
    )
    s3_secret_access_key: SecretStr | None = None
    s3_secret_access_key_file: Path | None = Field(
        default=None, description="File that holds the secret access key (instead of the value)."
    )
    data_dir: Path = Field(
        default=Path("data"), description="Runtime labels (labels.yaml) and feedback.sqlite3"
    )
    admin_token: SecretStr | None = Field(
        default=None,
        description="Bearer token for /v1/labels and /v1/feedback; unset disables them.",
    )
    oidc_issuer: str | None = Field(
        default=None,
        description="iss of the widget token: the public URL of synthwerk-identity. Required.",
    )
    oidc_audience: str = Field(
        default="synthwerk-api",
        description="Accepted aud values of the widget token, comma-separated.",
    )
    jwks_url: str | None = Field(
        default=None,
        description="Key set of the widget token: <identity>/.well-known/jwks.json. Required.",
    )
    auth_disabled: bool = Field(
        default=False,
        description="Local development only: no token on the classification and image routes. "
        "Refused when SYNTHWERK_PLATFORM_ENV is dev, stg or prd.",
    )
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    backend: Literal["siglip", "mobilenet"] = Field(
        default="siglip",
        description="siglip: open vocabulary (text labels). mobilenet: the 1000 ImageNet classes.",
    )
    siglip_dir: Path = Path("models/siglip2-base-patch16-224")
    label_cache_dir: Path = Path("models/label_cache")
    model_path: Path = Path("models/mobilenetv2-12.onnx")
    taxonomy_path: Path | None = Field(
        default=None,
        description="Topic tree YAML; None uses the packaged taxonomy for the backend.",
    )
    ort_threads: int = Field(
        default=0, ge=0, description="Threads per ONNX Runtime session; 0 = ORT default."
    )
    top_k: int = Field(default=5, ge=1, le=20)
    topic_min_score: float = Field(default=0.05, ge=0, le=1)
    uncertain_below: float = Field(
        default=0.5, ge=0, le=1, description="Flag a result when the best root topic scores lower."
    )

    @field_validator("cors_origins")
    @classmethod
    def _canonical_origins(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for index, item in enumerate(value):
            try:
                origin = canonical_origin(item.strip())
            except OriginError:
                raise ValueError(
                    f"item {index} is not an origin: use scheme://host[:port], "
                    "http only for localhost"
                ) from None
            if origin not in out:
                out.append(origin)
        return out

    @field_validator("identity_url")
    @classmethod
    def _identity_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parts = urlsplit(value.strip())
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.query
            or parts.fragment
            or parts.username is not None
        ):
            raise ValueError("must be an http(s) base URL, e.g. http://identity:8080")
        return value.strip().rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
