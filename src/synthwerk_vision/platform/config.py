from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        description="Browser origins allowed to call the API. Never use '*' in production.",
    )
    upload_dir: Path = Path("uploads")
    upload_quota_bytes: int = Field(
        default=1024 * 1024 * 1024,
        gt=0,
        description="Total bytes POST /v1/images may keep in upload_dir; more answers 507.",
    )
    upload_quota_files: int = Field(
        default=10_000,
        gt=0,
        description="Total files POST /v1/images may keep in upload_dir; more answers 507.",
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
