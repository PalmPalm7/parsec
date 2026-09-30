"""Cluster-wide Babylon listings must be paged, not one unbounded response.

Found on parsec-dev: listing every AnarchySubject on babylon prod in one
response, then filtering in Python, blocked the event loop past three liveness
probes and the kubelet restarted the pod mid-investigation.
"""

from __future__ import annotations

import asyncio

import pytest

import src.connections.babylon as conn
import src.tools.babylon as tools


class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class _PagedClient:
    """Serves `n` objects in pages of `limit`, with continue tokens."""

    def __init__(self, n: int, make) -> None:
        self.items = [make(i) for i in range(n)]
        self.calls: list[dict] = []

    async def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append(params)
        if "fieldSelector" in params:
            name = params["fieldSelector"].split("=", 1)[1]
            return _Resp({"items": [i for i in self.items if i["metadata"]["name"] == name]})
        start = int(params.get("continue") or 0)
        limit = int(params.get("limit") or len(self.items))
        page = self.items[start : start + limit]
        nxt = start + limit
        meta = {"continue": str(nxt)} if nxt < len(self.items) else {}
        return _Resp({"items": page, "metadata": meta})


def _subject(i: int) -> dict:
    return {
        "metadata": {"name": f"sub-{i:05d}", "namespace": "babylon-anarchy-x"},
        "spec": {"vars": {"job_vars": {"guid": f"g{i}"}}},
        "status": {"towerJobs": {}},
    }


@pytest.fixture
def client(monkeypatch):
    holder: dict = {}

    def install(n: int, make=_subject, page_size: int = 10) -> _PagedClient:
        c = _PagedClient(n, make)
        holder["c"] = c

        async def _get_client(_cluster):
            return c

        monkeypatch.setattr(conn, "_get_client", _get_client)
        monkeypatch.setattr(conn, "LIST_PAGE_SIZE", page_size)
        return c

    return install


def _collect(gen) -> list:
    async def run():
        return [x async for x in gen]

    return asyncio.run(run())


def test_iterator_follows_continue_tokens(client):
    c = client(35)
    items = _collect(conn.k8s_iter_cluster_wide("east", "g", "v1", "things", page_size=10))
    assert len(items) == 35
    assert [p.get("continue") for p in c.calls] == [None, "10", "20", "30"]
    assert all(p["limit"] == 10 for p in c.calls)


def test_iterator_max_items_bounds_the_scan(client):
    c = client(35)
    items = _collect(
        conn.k8s_iter_cluster_wide("east", "g", "v1", "things", page_size=10, max_items=12)
    )
    assert len(items) == 12
    assert len(c.calls) == 2


def test_subject_search_stops_fetching_once_it_has_enough(client):
    """A match on page one must not download the rest of the cluster."""
    c = client(1000, page_size=10)
    result = asyncio.run(tools._list_anarchy_subjects("east", "", "sub-0000", "", max_results=3))
    assert result["count"] == 3
    # One request, and a paged one: the old code also made one request — for
    # every object on the cluster.
    assert c.calls == [{"limit": 10}]


def test_subject_guid_search_finds_a_match_on_a_later_page(client):
    c = client(95, page_size=10)
    result = asyncio.run(tools._list_anarchy_subjects("east", "", "", "g87", max_results=5))
    assert [s["name"] for s in result["subjects"]] == ["sub-00087"]
    # Pages 1-9: the match is on page 9, and a GUID scan ends with that page.
    assert len(c.calls) == 9 and all(p["limit"] == 10 for p in c.calls)


def test_workshop_lookup_by_name_uses_a_field_selector(client, monkeypatch):
    c = client(500, make=lambda i: {"metadata": {"name": f"ws-{i}", "namespace": f"ns-{i}"}})

    async def fake_get_resource(cluster, group, version, plural, namespace, name):
        raise LookupError(f"{namespace}/{name}")  # stop right after the lookup

    monkeypatch.setattr(tools, "k8s_get_resource", fake_get_resource)
    result = asyncio.run(tools._get_workshop("east", "ws-321", ""))
    assert c.calls == [{"fieldSelector": "metadata.name=ws-321"}]
    assert "ns-321/ws-321" in result["error"]
