"""A controller or cluster that failed unrecoverably is not called again in the same turn.

Found on the staging pod: AAP2 controllers and Babylon clusters whose stored
credentials had been rotated answered 401 to every call, and nothing
remembered it — one question collected 47 AAP2 and 27 Babylon 401s, and a
babylon sub-agent kept calling clusters after a search had shown all six
failing. The old 401 text ("Authentication failed for controller 'X'") also
led the model to tell users the controller was not configured at all.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

import src.connections.aap2 as aap2
import src.connections.babylon as babylon
from src.connections import turn_state
from src.tools.aap2 import query_aap2
from src.tools.babylon import query_babylon_catalog

# The OS errors under httpx.ConnectError, chained the way httpcore and anyio
# chain them (see test_turn_state for the real stack producing these chains).
DNS_ERROR = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
REFUSED = OSError("All connection attempts failed")
REFUSED.__cause__ = ConnectionRefusedError(111, "Connect call failed ('10.0.0.7', 443)")

# Connect failures that can clear on the next call. ssl.SSLEOFError is given
# errno EAI_NONAME on purpose: on macOS a real one carries errno 8, which is
# EAI_NONAME there.
TRANSIENT = {
    "resolver timeout (EAI_AGAIN)": socket.gaierror(
        socket.EAI_AGAIN, "Temporary failure in name resolution"
    ),
    "expired certificate": ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired"
    ),
    "TLS handshake cut off": ssl.SSLEOFError(
        socket.EAI_NONAME, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation"
    ),
    "connect timeout": httpx.ConnectTimeout("timed out"),
}


@contextmanager
def turn() -> Iterator[None]:
    token = turn_state.begin_turn()
    try:
        yield
    finally:
        turn_state.end_turn(token)


class _Backend:
    """One fake controller or API server behind httpx.MockTransport.

    ``fails_with`` is the OS error under the ConnectError the request raises,
    or an httpx exception raised as is.
    """

    def __init__(
        self,
        status: int = 200,
        body: dict | None = None,
        dns_dead: bool = False,
        fails_with: BaseException | None = None,
    ):
        self.status = status
        self.body = body if body is not None else {}
        self.fails_with = DNS_ERROR if dns_dead else fails_with
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.fails_with, httpx.HTTPError):
            raise self.fails_with
        if self.fails_with is not None:
            raise httpx.ConnectError(str(self.fails_with), request=request) from self.fails_with
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
    assert msg.startswith("'east' did not resolve in DNS earlier in this investigation")
    assert "the host name in its configured URL does not exist" in msg
    assert "Retrying will not help" in msg
    assert "correct or remove this controller in Parsec's config" in msg
    assert "refused" not in msg
    assert "unverified" in msg


async def test_aap2_refused_connection_is_not_called_again(controllers):
    prod1 = controllers("prod1", fails_with=REFUSED)
    with turn():
        with pytest.raises(httpx.ConnectError):
            await aap2.api_get("prod1", "/api/v2/jobs/1/")
        with pytest.raises(httpx.ConnectError) as exc:
            await aap2.api_get("prod1", "/api/v2/jobs/2/")
    assert len(prod1.requests) == 1
    msg = str(exc.value)
    assert msg.startswith("'prod1' refused the connection earlier in this investigation")
    assert "nothing is accepting connections at its configured address" in msg
    assert "bring the controller back or correct its URL" in msg
    assert "DNS" not in msg
    assert "unverified" in msg


@pytest.mark.parametrize("cause", TRANSIENT.values(), ids=TRANSIENT.keys())
async def test_aap2_transient_connect_failure_is_retried_in_the_same_turn(controllers, cause):
    """A CoreDNS hiccup or a TLS error must not blank out a working controller for the turn."""
    prod0 = controllers("prod0", body={"id": 1}, fails_with=cause)
    with turn():
        with pytest.raises(httpx.TransportError):
            await aap2.api_get("prod0", "/api/v2/jobs/1/")
        prod0.fails_with = None
        assert await aap2.api_get("prod0", "/api/v2/jobs/1/") == {"id": 1}
        assert aap2.unavailable_reason("prod0") is None
    assert len(prod0.requests) == 2


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
    assert dns["error"].startswith("Cannot reach AAP2 controller: 'east' did not resolve in DNS")


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
    assert "did not resolve in DNS" in second["unavailable"]["west"]
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
    msg = str(exc.value)
    assert msg.startswith("Babylon cluster 'babydev' did not resolve in DNS earlier")
    assert "the host name of its API server does not exist" in msg
    assert "correct or remove this cluster's kubeconfig" in msg
    assert "refused" not in msg


async def test_babylon_refused_connection_is_not_called_again(clusters):
    east = clusters("east", fails_with=REFUSED)
    with turn():
        with pytest.raises(httpx.ConnectError):
            await babylon.k8s_get("east", "/version")
        with pytest.raises(httpx.ConnectError) as exc:
            await babylon.k8s_list("east", "anarchy.gpte.redhat.com", "v1", "anarchysubjects", "ns")
    assert len(east.requests) == 1
    msg = str(exc.value)
    assert msg.startswith("Babylon cluster 'east' refused the connection earlier")
    assert "nothing is accepting connections at its API server address" in msg
    assert "bring the API server back or correct its kubeconfig" in msg
    assert "DNS" not in msg


@pytest.mark.parametrize("cause", TRANSIENT.values(), ids=TRANSIENT.keys())
async def test_babylon_transient_connect_failure_is_retried_in_the_same_turn(clusters, cause):
    east = clusters("east", body={"kind": "PodList", "items": []}, fails_with=cause)
    with turn():
        with pytest.raises(httpx.TransportError):
            await babylon.k8s_get("east", "/api/v1/namespaces/x/pods")
        east.fails_with = None
        assert (await babylon.k8s_get("east", "/api/v1/namespaces/x/pods"))["kind"] == "PodList"
    assert len(east.requests) == 2


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
