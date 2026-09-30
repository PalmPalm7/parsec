"""Health check endpoints."""

import asyncio
import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import src.connections.reporting_mcp as reporting_mcp

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])

#: Minimum gap between background retries of Reporting MCP discovery.
_MCP_RETRY_INTERVAL_S = 30.0
_mcp_retry: "asyncio.Task[str] | None" = None
_mcp_retry_at = 0.0


def _retry_mcp_discovery() -> None:
    """Retry Reporting MCP discovery in the background, at most every 30s.

    Startup runs discovery once and nothing else retries it. Now that a
    not-ready pod answers 503 and leaves the Service, a Reporting MCP blip at
    startup would otherwise keep the pod out for good — liveness still passes,
    so the kubelet never restarts it. The probe itself never waits on the MCP.
    """
    global _mcp_retry, _mcp_retry_at
    if _mcp_retry is not None and not _mcp_retry.done():
        return
    now = time.monotonic()
    if _mcp_retry_at and now - _mcp_retry_at < _MCP_RETRY_INTERVAL_S:
        return
    _mcp_retry_at = now
    logger.info("Reporting MCP not initialized — retrying discovery in the background")
    _mcp_retry = asyncio.create_task(reporting_mcp.fetch_server_instructions())


@router.get("/health")
async def health():
    """Liveness probe."""
    return {"status": "ok"}


@router.get("/health/ready", responses={503: {"description": "Not ready"}})
async def readiness():
    """Readiness probe — checks MCP init succeeded, doesn't wait on it.

    Not ready is a 503: the kubelet reads only the status code, so the 200 this
    used to return with a not_ready body kept a pod without its DB tools in
    the Service.
    """
    if not reporting_mcp.get_mcp_url():
        return {"status": "ready", "db": "reporting_mcp_not_configured"}

    if reporting_mcp.get_server_instructions() or reporting_mcp.get_mcp_tools():
        return {"status": "ready", "db": "via_reporting_mcp"}

    _retry_mcp_discovery()
    return JSONResponse(
        status_code=503,
        content={"status": "not_ready", "db": "reporting_mcp_not_initialized"},
    )
