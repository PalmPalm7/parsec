"""Alert investigation endpoint — POST /api/alert/investigate."""

import hmac
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from src.agent.orchestrator import run_alert_investigation
from src.config import get_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/alert", tags=["alert"])


class AlertRequest(BaseModel):
    alert_type: str
    account_id: str
    alert_text: str
    account_name: str = ""
    user_arn: str = ""
    event_time: str = ""
    region: str = ""
    event_details: dict | None = None


class AlertResponse(BaseModel):
    should_alert: bool
    severity: str
    summary: str
    investigation_log: str
    duration_seconds: float


async def _require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Reject a caller without the configured X-API-Key.

    A dependency rather than a check in the handler body because FastAPI runs
    dependencies before it validates the request body: done in the handler, an
    unauthenticated caller with a bad body got a 422 that spelled out the
    request schema instead of a 401. compare_digest keeps the comparison
    constant-time; both sides are bytes because it refuses non-ASCII str.

    The configured key is coerced to str first: Dynaconf casts an all-digit
    PARSEC_ALERT_API_KEY to an int, which has no .encode(), so every keyed
    request would be a 500 instead of an answer.
    """
    configured_key = str(get_config().get("alert_api_key", "") or "")

    if not configured_key:
        raise HTTPException(
            status_code=503,
            detail="Alert investigation endpoint is not configured (alert_api_key is empty)",
        )

    if not x_api_key or not hmac.compare_digest(x_api_key.encode(), configured_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@router.post(
    "/investigate",
    response_model=AlertResponse,
    responses={401: {"description": "Unauthorized"}, 503: {"description": "Service Unavailable"}},
    dependencies=[Depends(_require_api_key)],
)
async def investigate_alert(body: AlertRequest):
    """Investigate an alert and return a structured verdict.

    Authenticated via X-API-Key header (not OAuth — called by Lambda).
    """
    logger.info(
        "Alert investigation request: type=%s account=%s",
        body.alert_type,
        body.account_id,
    )

    start = time.monotonic()
    try:
        result = await run_alert_investigation(
            alert_type=body.alert_type,
            account_id=body.account_id,
            alert_text=body.alert_text,
            account_name=body.account_name,
            user_arn=body.user_arn,
            event_time=body.event_time,
            region=body.region,
            event_details=body.event_details,
        )
    except Exception:
        logger.exception("Alert investigation failed")
        elapsed = round(time.monotonic() - start, 1)
        result = {
            "should_alert": True,
            "severity": "medium",
            "summary": "Investigation encountered an unexpected error — alerting as a precaution.",
            "investigation_log": "",
            "duration_seconds": elapsed,
        }

    return AlertResponse(**result)
