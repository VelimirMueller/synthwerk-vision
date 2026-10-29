"""Widget token gate for the classification and image routes (synthwerk#76).

`Authorization: Bearer <widget token>` is required. The token comes from synthwerk-identity
(`POST /v1/apps/{appId}/widget-tokens`, synthwerk#30) and is bound to one origin: the browser
`Origin` header must equal its `synthwerk:origin` claim, as in synthwerk-llm.

| Case | Answer |
|---|---|
| No token, bad token, not a widget token | 401 |
| Token valid, `Origin` missing, different or sent twice | 403 |
| Key set cannot be loaded | 503 |

The body never names the reason or echoes the token. The reason goes to the log as constant text.
"""

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from synthwerk_vision.platform.auth import (
    AuthConfigError,
    KeysUnavailable,
    TokenVerifier,
    Unauthenticated,
    WidgetPrincipal,
)
from synthwerk_vision.platform.config import Settings
from synthwerk_vision.schemas import ErrorOut

logger = logging.getLogger("synthwerk_vision")

_bearer = HTTPBearer(auto_error=False)
_DEV_ONLY_ENVS = {"local", "ci"}

TOKEN_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorOut, "description": "No valid widget token"},
    403: {"model": ErrorOut, "description": "Origin does not match the widget token"},
}


def build_verifier(settings: Settings) -> TokenVerifier | None:
    """The token verifier, or None when auth is disabled. Raises AuthConfigError when the
    settings are missing, so the service does not start open by mistake."""
    if settings.auth_disabled:
        if settings.platform_env not in _DEV_ONLY_ENVS:
            raise AuthConfigError(
                f"SYNTHWERK_VISION_AUTH_DISABLED is not allowed in {settings.platform_env}"
            )
        logger.warning(
            "auth disabled: classification and image routes need no token",
            extra={"event.name": "vision.auth.disabled"},
        )
        return None
    missing = [
        name
        for name, value in (
            ("SYNTHWERK_VISION_OIDC_ISSUER", settings.oidc_issuer),
            ("SYNTHWERK_VISION_JWKS_URL", settings.jwks_url),
        )
        if not value
    ]
    if missing:
        raise AuthConfigError(
            f"{' and '.join(missing)} required (or SYNTHWERK_VISION_AUTH_DISABLED=true locally)"
        )
    assert settings.oidc_issuer is not None and settings.jwks_url is not None  # noqa: S101
    return TokenVerifier(
        issuer=settings.oidc_issuer,
        audiences=[a.strip() for a in settings.oidc_audience.split(",") if a.strip()],
        jwks_url=settings.jwks_url,
    )


def _refuse(reason: str, has_token: bool) -> HTTPException:
    logger.info(
        "widget token refused",
        extra={"event.name": "vision.auth.refused", "synthwerk.auth.reason": reason},
    )
    challenge = 'Bearer error="invalid_token"' if has_token else "Bearer"
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "a valid widget token is required",
        headers={"WWW-Authenticate": challenge},
    )


async def require_widget_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> WidgetPrincipal | None:
    """FastAPI dependency: the verified caller, or None when auth is disabled."""
    verifier: TokenVerifier | None = request.app.state.verifier
    if verifier is None:
        return None
    if credentials is None or not credentials.credentials:
        raise _refuse("no bearer token", has_token=False)
    try:
        principal = await verifier.verify(credentials.credentials)
    except Unauthenticated as exc:
        raise _refuse(exc.reason, has_token=True) from exc
    except KeysUnavailable as exc:
        logger.warning(
            "widget token keys unavailable", extra={"event.name": "vision.auth.unavailable"}
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "token keys are not available, try again later"
        ) from exc
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0] != principal.origin:
        logger.info(
            "widget token refused",
            extra={"event.name": "vision.auth.refused", "synthwerk.auth.reason": "origin"},
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "origin does not match the widget token")
    return principal


WidgetToken = Annotated[WidgetPrincipal | None, Depends(require_widget_token)]
