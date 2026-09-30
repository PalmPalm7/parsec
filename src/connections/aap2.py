"""AAP2 controller connections — httpx-based REST API clients."""

import logging
from urllib.parse import urlparse

import httpx

from src.config import get_config
from src.connections import turn_state

logger = logging.getLogger(__name__)

# Parsed cluster configs: {name: {url, username, password}}
_cluster_configs: dict[str, dict[str, str]] = {}
# Cached clients keyed by cluster name
_clients: dict[str, httpx.AsyncClient] = {}


def init_aap2() -> None:
    """Initialize AAP2 controller connections from config."""
    cfg = get_config()
    aap2_cfg = cfg.get("aap2", {})
    clusters = aap2_cfg.get("clusters", {})

    if not clusters:
        logger.info("No AAP2 controllers configured — job lookups disabled")
        return

    for name, cluster_cfg in clusters.items():
        name_lower = name.lower()
        if not isinstance(cluster_cfg, dict):
            continue
        url = cluster_cfg.get("url", "") or cluster_cfg.get("URL", "")
        username = cluster_cfg.get("username", "") or cluster_cfg.get("USERNAME", "")
        password = cluster_cfg.get("password", "") or cluster_cfg.get("PASSWORD", "")  # noqa: S105
        if not url:
            logger.warning("AAP2 cluster '%s' has no URL", name_lower)
            continue
        if not username or not password:
            logger.warning("AAP2 cluster '%s' has no credentials", name_lower)
            continue

        _cluster_configs[name_lower] = {
            "url": url.rstrip("/"),
            "username": username,
            "password": password,
        }
        logger.info("AAP2 cluster '%s' configured (url=%s)", name_lower, url)

    logger.info("AAP2: %d controllers configured", len(_cluster_configs))


def get_configured_controllers() -> list[str]:
    """Return list of configured AAP2 controller names."""
    return list(_cluster_configs.keys())


def resolve_controller(controller: str) -> str:
    """Resolve a controller input to a configured cluster name.

    Accepts:
      - Short name: "east" -> exact match against config keys
      - Full hostname: "aap2-prod-us-east-2.aap.infra.demo.redhat.com"
        -> contains match against configured URLs

    Returns the cluster name, or raises ValueError if not found.
    """
    if not controller:
        raise ValueError(
            "No controller specified. " f"Configured: {', '.join(_cluster_configs.keys())}"
        )

    key = controller.lower().strip()

    # Exact match on cluster name
    if key in _cluster_configs:
        return key

    # Contains match on URL hostname
    for name, cfg in _cluster_configs.items():
        parsed = urlparse(cfg["url"])
        hostname = (parsed.hostname or "").lower()
        if key in hostname or hostname in key:
            return name

    raise ValueError(
        f"Unknown AAP2 controller: '{controller}'. "
        f"Configured: {', '.join(_cluster_configs.keys())}"
    )


async def _get_client(cluster_name: str) -> httpx.AsyncClient:
    """Get or create an httpx client for an AAP2 controller."""
    if cluster_name in _clients:
        return _clients[cluster_name]

    if cluster_name not in _cluster_configs:
        raise ValueError(
            f"Unknown AAP2 controller: '{cluster_name}'. "
            f"Configured: {list(_cluster_configs.keys())}"
        )

    cfg = _cluster_configs[cluster_name]
    client = httpx.AsyncClient(
        base_url=cfg["url"],
        auth=httpx.BasicAuth(cfg["username"], cfg["password"]),
        timeout=30.0,
        headers={"Accept": "application/json"},
    )
    _clients[cluster_name] = client
    return client


def _target(cluster_name: str) -> str:
    return f"aap2:{cluster_name}"


def _unavailable_error(cluster_name: str) -> Exception | None:
    """The error to raise, without a request, for a controller already dead this turn.

    Once a controller has rejected Parsec's credentials, or its host name did
    not resolve or it refused the connection, every retry fails the same way
    until an operator acts. On
    the staging pod one question collected 47 AAP2 401s this way. The
    exception type is the one the first failure raised: PermissionError keeps
    the debug routes' 502 mapping, ConnectError keeps query_aap2's
    "Cannot reach" handling.
    """
    reason = turn_state.dead_reason(_target(cluster_name))
    if reason is None:
        return None
    if reason == turn_state.CREDENTIALS_REJECTED:
        return PermissionError(
            f"AAP2 controller '{cluster_name}' rejected Parsec's stored credentials (HTTP 401) "
            "earlier in this investigation, so Parsec did not call it again. Retrying will not "
            "help; an operator has to update the credentials. Report anything that depends on "
            "this controller as unverified."
        )
    # Each cause needs a different fix, so say which one it was instead of
    # one sentence covering both.
    if reason == turn_state.HOST_NOT_FOUND:
        what = (
            "did not resolve in DNS earlier in this investigation, so Parsec did not call it "
            "again: the host name in its configured URL does not exist. Retrying will not help; "
            "an operator has to correct or remove this controller in Parsec's config."
        )
    else:  # turn_state.CONNECTION_REFUSED, the only other reason recorded here
        what = (
            "refused the connection earlier in this investigation, so Parsec did not call it "
            "again: nothing is accepting connections at its configured address. Retrying will "
            "not help; an operator has to bring the controller back or correct its URL."
        )
    return httpx.ConnectError(
        f"'{cluster_name}' {what} Report anything that depends on this controller as unverified."
    )


def unavailable_reason(cluster_name: str) -> str | None:
    """Why ``cluster_name`` is not called again in this turn, or ``None``."""
    error = _unavailable_error(cluster_name)
    return str(error) if error else None


def _check_response(resp: httpx.Response, cluster_name: str, path: str) -> None:
    """Raise clear errors for common HTTP failure codes."""
    if resp.status_code == 401:
        # The old "Authentication failed for controller 'X'" led the model to
        # tell users the controller was not configured and to offer to add
        # credentials for it. It is configured; its credentials went stale.
        turn_state.mark_dead(_target(cluster_name), turn_state.CREDENTIALS_REJECTED)
        raise PermissionError(
            f"Parsec's configured credentials for AAP2 controller '{cluster_name}' were "
            "rejected (HTTP 401) — likely expired or rotated. The controller is configured; "
            "retrying will not help, and an operator has to update the credentials."
        )
    if resp.status_code == 404:
        raise LookupError(f"Not found on controller '{cluster_name}': {path} (HTTP 404)")
    resp.raise_for_status()


async def _get(
    cluster_name: str,
    path: str,
    params: dict | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """GET from a controller unless it already proved unusable in this turn."""
    error = _unavailable_error(cluster_name)
    if error is not None:
        raise error
    client = await _get_client(cluster_name)
    try:
        resp = await client.get(path, params=params or {}, headers=headers)
    except httpx.ConnectError as e:
        # On staging the east and west controllers no longer resolve at all.
        # Only such final causes are remembered: a resolver hiccup or a TLS
        # failure is left for the next call to retry.
        reason = turn_state.permanent_connect_failure(e)
        if reason is not None:
            turn_state.mark_dead(_target(cluster_name), reason)
        raise
    _check_response(resp, cluster_name, path)
    return resp


async def api_get(cluster_name: str, path: str, params: dict | None = None) -> dict:
    """Make a GET request to the AAP2 REST API.

    Returns the JSON response body. Raises on HTTP errors with clear messages.
    """
    resp = await _get(cluster_name, path, params)
    return resp.json()


async def api_get_text(cluster_name: str, path: str, params: dict | None = None) -> str:
    """Make a GET request expecting plain text (e.g. job stdout).

    Overrides the default Accept header to request text/plain.
    """
    resp = await _get(cluster_name, path, params, headers={"Accept": "text/plain"})
    return resp.text


async def api_paginate(
    cluster_name: str,
    path: str,
    params: dict | None = None,
    max_results: int = 50,
) -> list[dict]:
    """Paginate through AAP2 API results.

    The AAP2 API returns paginated responses with 'next' URLs.
    Collects up to max_results items.
    """
    params = dict(params or {})
    params.setdefault("page_size", min(max_results, 200))

    results: list[dict] = []
    data = await api_get(cluster_name, path, params)
    results.extend(data.get("results", []))

    while len(results) < max_results and data.get("next"):
        next_url = data["next"]
        parsed = urlparse(next_url)
        next_path = parsed.path
        if parsed.query:
            next_path += f"?{parsed.query}"

        data = await api_get(cluster_name, next_path)
        results.extend(data.get("results", []))

    return results[:max_results]


async def close_clients() -> None:
    """Close all httpx clients."""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()
