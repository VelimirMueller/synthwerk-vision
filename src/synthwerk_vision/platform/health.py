"""`/healthz` and `/readyz` (conventions §2), shaped by `openapi/_shared/health.yaml` in
synthwerk-contracts.

The app puts its readiness checks on `app.state.readiness_checks`; this module only runs
them. Replace it with the `synthwerk_contracts` kit when that is published.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from synthwerk_vision import __version__

Status = Literal["ok", "degraded", "down"]
_RANK: dict[Status, int] = {"ok": 0, "degraded": 1, "down": 2}


class Liveness(BaseModel):
    status: Literal["ok"] = "ok"


class Check(BaseModel):
    name: Annotated[str, Field(max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]
    status: Status
    latency_ms: Annotated[int, Field(ge=0, le=60000)]
    detail: Annotated[str | None, Field(max_length=256)] = None


class Readiness(BaseModel):
    status: Status
    version: Annotated[str, Field(max_length=64)]
    commit: Annotated[str, Field(max_length=40, pattern=r"^[0-9a-f]{7,40}$")]
    checks: Annotated[list[Check], Field(max_length=32)]


@dataclass(frozen=True)
class CheckResult:
    status: Status
    detail: str | None = None  # short, constant text; never secrets or user data


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    run: Callable[[Request], Awaitable[CheckResult]]
    timeout_s: float = 0.25  # the sum over all checks stays below the 1 s budget


async def _run(check: ReadinessCheck, request: Request) -> Check:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(check.run(request), check.timeout_s)
    except TimeoutError:
        result = CheckResult("down", "timeout")
    except Exception:
        result = CheckResult("down", "check failed")
    latency_ms = round((time.perf_counter() - started) * 1000)
    return Check(name=check.name, status=result.status, latency_ms=latency_ms, detail=result.detail)


router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> Liveness:
    """Liveness: the process runs. No dependency checks."""
    return Liveness()


@router.get(
    "/readyz",
    response_model=Readiness,
    response_model_exclude_none=True,
    responses={503: {"model": Readiness, "description": "Status `down`. Do not send traffic."}},
)
async def readyz(request: Request) -> JSONResponse:
    """Readiness: 200 when `ok` or `degraded`, 503 when `down`."""
    checks_to_run: list[ReadinessCheck] = request.app.state.readiness_checks
    checks = list(await asyncio.gather(*(_run(check, request) for check in checks_to_run)))
    worst: Status = max((check.status for check in checks), key=_RANK.__getitem__, default="ok")
    body = Readiness(
        status=worst, version=__version__, commit=request.app.state.commit, checks=checks
    )
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=503 if worst == "down" else 200
    )
