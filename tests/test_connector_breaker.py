"""A controller or cluster that failed unrecoverably is not called again in the same turn.

Found on the staging pod: AAP2 controllers and Babylon clusters whose stored
credentials had been rotated answered 401 to every call, and nothing
remembered it — one question collected 47 AAP2 and 27 Babylon 401s, and a
babylon sub-agent kept calling clusters after a search had shown all six
failing. The old 401 text ("Authentication failed for controller 'X'") also
led the model to tell users the controller was not configured at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

import src.connections.aap2 as aap2
import src.connections.babylon as babylon
from src.connections import turn_state
from src.tools.aap2 import query_aap2
from src.tools.babylon import query_babylon_catalog

DNS_ERROR = "[Errno -2] Name or service not known"


@contextmanager
def turn() -> Iterator[None]:
    token = turn_state.begin_turn()
    try:
        yield
    finally:
        turn_state.end_turn(token)


class _Backend:
    """One fake controller or API server behind httpx.MockTransport."""

    def __init__(self, status: int = 200, body: dict | None = None, dns_dead: bool = False):
        self.status = status
        self.body = body if body is not None else {}
        self.dns_dead = dns_dead
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.dns_dead:
            raise httpx.ConnectError(DNS_ERROR, request=request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def controllers(monkeypatch):
    monkeypatch.setattr(aap2, "_cluster_configs", {})
    monkeypatch.setattr(aap2, "_clients", {})

    def install(name: str, **kwargs) -> _Backend:
        backend = _Backend(**kwargs)
        url = f"https://{name}.aap.example.com"
        aap2._cluster_configs[name] = {"url": url, "username": "u", "password": "p"}
        aap2._clients[name] = httpx.AsyncClient(
            base_url=url, transport=httpx.MockTransport(backend.handle)
        )
        return backend

    return install


@pytest.fixture
def clusters(monkeypatch):
    monkeypatch.setattr(babylon, "_cluster_configs", {})
    monkeypatch.setattr(babylon, "_clients", {})

    def install(name: str, **kwargs) -> _Backend:
        backend = _Backend(**kwargs)
        server = f"https://api.{name}.example.com:6443"
        babylon._cluster_configs[name] = {"server": server, "token": "t", "verify_ssl": True}
        babylon._clients[name] = httpx.AsyncClient(
            base_url=server, transport=httpx.MockTransport(backend.handle)
        )
        return backend

    return install


# ---------------------------------------------------------------------------
# AAP2
# ---------------------------------------------------------------------------


async def test_aap2_401_says_the_configured_credentials_were_rejected(controllers):
    controllers("partner0", status=401)
    with pytest.raises(PermissionError) as exc:
        await aap2.api_get("partner0", "/api/v2/jobs/1/")
    msg = str(exc.value)
    assert "configured credentials for AAP2 controller 'partner0' were rejected" in msg
    assert "likely expired or rotated" in msg
    assert "retrying will not help" in msg
    assert "Authentication failed" not in msg


async def test_aap2_second_call_in_the_same_turn_makes_no_request(controllers):
    prod0 = controllers("prod0", status=401)
    with turn():
        with pytest.raises(PermissionError):
            await aap2.api_get("prod0", "/api/v2/jobs/1/")
        # Still PermissionError, so the debug routes keep answering 502.
        with pytest.raises(PermissionError) as exc:
            await aap2.api_get_text("prod0", "/api/v2/jobs/1/stdout/")
    assert len(prod0.requests) == 1
    msg = str(exc.value)
    assert "rejected Parsec's stored credentials (HTTP 401) earlier in this investigation" in msg
    assert "Retrying will not help" in msg
    assert "operator" in msg
    assert "unverified" in msg


async def test_aap2_a_new_turn_tries_again(controllers):
    prod0 = controllers("prod0", status=401)
    with turn(), pytest.raises(PermissionError):
        await aap2.api_get("prod0", "/api/v2/jobs/1/")
    prod0.status, prod0.body = 200, {"id": 1}  # an operator rotated the credentials
    with turn():
        assert await aap2.api_get("prod0", "/api/v2/jobs/1/") == {"id": 1}
    assert len(prod0.requests) == 2


async def test_aap2_outside_a_turn_nothing_is_remembered(controllers):
    prod0 = controllers("prod0", status=401)
    for _ in range(2):
        with pytest.raises(PermissionError):
            await aap2.api_get("prod0", "/api/v2/jobs/1/")
    assert len(prod0.requests) == 2


async def test_aap2_unresolvable_host_is_not_called_again(controllers):
    east = controllers("east", dns_dead=True)
    with turn():
        with pytest.raises(httpx.ConnectError):
            await aap2.api_get("east", "/api/v2/jobs/1/")
        with pytest.raises(httpx.ConnectError) as exc:
            await aap2.api_get("east", "/api/v2/jobs/2/")
    assert len(east.requests) == 1
    msg = str(exc.value)
    assert DNS_ERROR in msg
    assert "does not resolve" in msg
    assert "Retrying will not help" in msg
    assert "operator" in msg


async def test_query_aap2_answers_a_dead_controller_from_memory(controllers):
    prod0 = controllers("prod0", status=401)
    east = controllers("east", dns_dead=True)
    with turn():
        await query_aap2("get_job", controller="prod0", job_id=1)
        await query_aap2("get_job", controller="east", job_id=1)
        auth = await query_aap2("get_job_log", controller="prod0", job_id=1)
        dns = await query_aap2("get_job_events", controller="east", job_id=1)
    assert len(prod0.requests) == 1 and len(east.requests) == 1
    assert "earlier in this investigation" in auth["error"]
    assert dns["error"].startswith("Cannot reach AAP2 controller: 'east' was already unreachable")


def _jobs_page(job_id: int) -> dict:
    job = {"id": job_id, "name": "deploy", "status": "failed", "finished": "2026-09-30T18:00Z"}
    return {"results": [job], "next": None}


async def test_find_jobs_skips_dead_controllers_and_reports_them(controllers):
    prod0 = controllers("prod0", status=401)
    west = controllers("west", dns_dead=True)
    east = controllers("east", body=_jobs_page(7))
    with turn():
        first = await query_aap2("find_jobs")
        second = await query_aap2("find_jobs")

    # The first fan-out learns which controllers are dead and says so, instead
    # of returning east's jobs as if the others had nothing.
    assert [j["job_id"] for j in first["jobs"]] == [7]
    assert sorted(e.split(":")[0] for e in first["errors"]) == ["prod0", "west"]

    # The second one does not ask them again.
    assert len(prod0.requests) == 1 and len(west.requests) == 1 and len(east.requests) == 2
    assert [j["job_id"] for j in second["jobs"]] == [7]
    assert set(second["unavailable"]) == {"prod0", "west"}
    assert "stored credentials (HTTP 401)" in second["unavailable"]["prod0"]
    assert "does not resolve" in second["unavailable"]["west"]
    assert "errors" not in second


async def test_find_jobs_when_every_controller_is_dead(controllers):
    prod0 = controllers("prod0", status=401)
    with turn():
        await query_aap2("find_jobs")
        result = await query_aap2("find_jobs")
    assert len(prod0.requests) == 1
    assert "error" in result
    assert set(result["unavailable"]) == {"prod0"}


# ---------------------------------------------------------------------------
# Babylon
# ---------------------------------------------------------------------------


async def test_babylon_401_is_explained_and_not_retried_in_the_turn(clusters):
    east = clusters("east", status=401)
    with turn():
        with pytest.raises(httpx.HTTPStatusError) as first:
            await babylon.k8s_get("east", "/api/v1/namespaces/x/pods")
        with pytest.raises(PermissionError) as later:
            await babylon.k8s_list("east", "anarchy.gpte.redhat.com", "v1", "anarchysubjects", "ns")
        with pytest.raises(PermissionError):
            await babylon.k8s_get_resource("east", "", "v1", "pods", "ns", "p")
        with pytest.raises(PermissionError):
            await babylon.k8s_list_cluster_wide("east", "g", "v1", "things")
        with pytest.raises(PermissionError):
            async for _ in babylon.k8s_iter_cluster_wide("east", "g", "v1", "things"):
                pass
    assert len(east.requests) == 1
    assert first.value.response.status_code == 401
    assert "configured token for Babylon cluster 'east' was rejected" in str(first.value)
    assert "retrying will not help" in str(first.value)
    msg = str(later.value)
    assert "earlier in this investigation" in msg
    assert "Retrying will not help" in msg
    assert "operator" in msg


async def test_babylon_unresolvable_cluster_is_not_called_again(clusters):
    babydev = clusters("babydev", dns_dead=True)
    with turn():
        with pytest.raises(httpx.ConnectError):
            await babylon.k8s_get("babydev", "/version")
        with pytest.raises(httpx.ConnectError) as exc:
            await babylon.k8s_get_text("babydev", "/api/v1/namespaces/x/pods/p/log")
    assert len(babydev.requests) == 1
    assert "does not resolve" in str(exc.value)
    assert DNS_ERROR in str(exc.value)


async def test_babylon_new_turn_and_no_turn_both_call_out(clusters):
    east = clusters("east", status=401)
    with turn(), pytest.raises(httpx.HTTPStatusError):
        await babylon.k8s_get("east", "/version")
    with turn(), pytest.raises(httpx.HTTPStatusError):
        await babylon.k8s_get("east", "/version")
    with pytest.raises(httpx.HTTPStatusError):
        await babylon.k8s_get("east", "/version")
    with pytest.raises(httpx.HTTPStatusError):
        await babylon.k8s_get("east", "/version")
    assert len(east.requests) == 4


async def test_babylon_guid_search_across_dead_clusters_makes_no_requests(clusters):
    """Staging q04: after one all-cluster GUID search failed, the sub-agent searched again."""
    dead = [clusters("east", status=401), clusters("babydev", dns_dead=True)]
    with turn():
        await query_babylon_catalog(action="list_anarchy_subjects", guid="abcde")
        again = await query_babylon_catalog(action="list_anarchy_subjects", guid="abcde")
    assert [len(c.requests) for c in dead] == [1, 1]
    assert all("earlier in this investigation" in e for e in again["errors"])
