"""Babylon AnarchySubject lookups must not read whole clusters when they need not.

On the live pods a GUID search read every page of every cluster, one cluster
after another, because it stopped only after 50 matches and a GUID matches one
provision. Dev q13's agents already had the subject's name and namespace from
the provisions row and still ran two such scans, and ``account_id`` was
silently ignored, so each of staging q06's six ``account_id`` calls listed a
whole cluster.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import src.connections.babylon as conn
import src.tools.babylon as tools

_NS = "babylon-anarchy-3"


def _subject(i: int) -> dict:
    return {
        "metadata": {"name": f"agd-v2.rhacs-demo-cnv.prod-g{i:05d}", "namespace": _NS},
        "spec": {"vars": {"job_vars": {"guid": f"g{i:05d}"}}},
        "status": {"towerJobs": {}},
    }


class _Cluster:
    """A fake Babylon API: paged cluster-wide lists, field selectors, namespaced GETs."""

    def __init__(self, n: int = 0, make=_subject, block: bool = False, gauge=None) -> None:
        self.items = [make(i) for i in range(n)]
        self.block = block
        self.gauge = gauge
        self.requests: list[tuple[str, dict]] = []

    async def get(self, path, params=None):
        params = dict(params or {})
        self.requests.append((path, params))
        if self.block:
            await asyncio.Event().wait()  # never answers; only cancellation ends it
        if self.gauge is not None:
            await self.gauge.hold()
        request = httpx.Request("GET", f"https://api.test{path}")
        if "/namespaces/" in path:
            namespace, _plural, name = path.split("/namespaces/", 1)[1].split("/")
            for item in self.items:
                meta = item["metadata"]
                if (meta["namespace"], meta["name"]) == (namespace, name):
                    return httpx.Response(200, request=request, json=item)
            return httpx.Response(404, request=request, json={"kind": "Status", "code": 404})
        if "fieldSelector" in params:
            name = params["fieldSelector"].split("=", 1)[1]
            found = [i for i in self.items if i["metadata"]["name"] == name]
            return httpx.Response(200, request=request, json={"items": found, "metadata": {}})
        start = int(params.get("continue") or 0)
        limit = int(params.get("limit") or len(self.items))
        page = self.items[start : start + limit]
        meta = {"continue": str(start + limit)} if start + limit < len(self.items) else {}
        return httpx.Response(200, request=request, json={"items": page, "metadata": meta})


class _Gauge:
    """Counts how many clusters are being queried at the same moment."""

    def __init__(self) -> None:
        self.now = 0
        self.peak = 0

    async def hold(self) -> None:
        self.now += 1
        self.peak = max(self.peak, self.now)
        await asyncio.sleep(0.01)
        self.now -= 1


@pytest.fixture
def clusters(monkeypatch):
    def install(page_size: int = 250, **by_name: _Cluster) -> None:
        async def _get_client(name):
            return by_name[name]

        monkeypatch.setattr(conn, "_get_client", _get_client)
        monkeypatch.setattr(conn, "LIST_PAGE_SIZE", page_size)
        monkeypatch.setattr(tools, "get_configured_clusters", lambda: list(by_name))

    return install


def _query(**kwargs) -> dict:
    return asyncio.run(tools.query_babylon_catalog("list_anarchy_subjects", **kwargs))


def test_guid_found_on_the_first_page_does_not_read_the_rest_of_the_cluster(clusters):
    east = _Cluster(5000)
    clusters(east=east)
    result = _query(cluster="east", guid="g00010")
    assert [s["name"] for s in result["subjects"]] == ["agd-v2.rhacs-demo-cnv.prod-g00010"]
    assert len(east.requests) <= 2  # was 20 pages: the whole cluster
    assert result["complete"] is False and "first GUID match" in result["note"]


def test_guid_miss_stops_at_the_scan_cap_and_says_so(clusters, monkeypatch):
    monkeypatch.setattr(tools, "_SUBJECT_SCAN_MAX_ITEMS", 50)
    east = _Cluster(200)
    clusters(page_size=10, east=east)
    result = _query(cluster="east", guid="nomatch")
    assert result["count"] == 0
    assert result["complete"] is False and "Examined 50" in result["note"]
    assert len(east.requests) == 5


def test_capped_miss_on_every_cluster_is_incomplete_not_absent(clusters, monkeypatch):
    monkeypatch.setattr(tools, "_SUBJECT_SCAN_MAX_ITEMS", 50)
    clusters(page_size=10, east=_Cluster(200), west=_Cluster(3))
    result = _query(guid="nomatch")
    assert result["count"] == 0 and result["incomplete"] is True
    assert result["partially_searched_clusters"] == ["east"]


def test_a_scan_that_reaches_the_end_is_complete(clusters):
    clusters(page_size=10, east=_Cluster(25))
    result = _query(cluster="east", guid="nomatch")
    assert result["count"] == 0 and result["complete"] is True and "note" not in result


def test_name_and_namespace_is_one_get(clusters):
    east = _Cluster(5000)
    clusters(east=east)
    name = "agd-v2.rhacs-demo-cnv.prod-g04321"
    result = _query(cluster="east", name=name, namespace=_NS)
    assert [s["name"] for s in result["subjects"]] == [name]
    assert east.requests == [
        (f"/apis/anarchy.gpte.redhat.com/v1/namespaces/{_NS}/anarchysubjects/{name}", {})
    ]


def test_name_alone_uses_a_field_selector(clusters):
    east = _Cluster(5000)
    clusters(east=east)
    name = "agd-v2.rhacs-demo-cnv.prod-g04321"
    result = _query(cluster="east", name=name)
    assert result["count"] == 1
    assert east.requests == [
        (
            "/apis/anarchy.gpte.redhat.com/v1/anarchysubjects",
            {"fieldSelector": f"metadata.name={name}"},
        )
    ]


def test_name_lookup_without_a_cluster_asks_each_cluster_once(clusters):
    east, west = _Cluster(0), _Cluster(5000)
    clusters(east=east, west=west)
    name = "agd-v2.rhacs-demo-cnv.prod-g04321"
    result = _query(name=name, namespace=_NS)
    assert result["cluster"] == "west" and result["count"] == 1
    assert len(east.requests) <= 1 and len(west.requests) == 1


def test_name_missing_everywhere_is_a_clean_miss(clusters):
    clusters(east=_Cluster(0), west=_Cluster(3))
    result = _query(name="no-such-subject", namespace=_NS)
    assert "error" not in result and result["count"] == 0 and "incomplete" not in result


@pytest.mark.parametrize("cluster", ["east", ""])
def test_account_id_is_refused_without_listing_anything(clusters, cluster):
    east = _Cluster(5000)
    clusters(east=east)
    result = _query(cluster=cluster, account_id="549444779659")
    assert "cannot filter by account_id" in result["error"]
    assert "anarchy_subject_name" in result["error"]
    assert east.requests == []


def test_clusters_are_searched_concurrently(clusters):
    gauge = _Gauge()
    clusters(**{f"c{i}": _Cluster(3, gauge=gauge) for i in range(6)})
    result = _query(guid="nomatch")
    assert result["count"] == 0 and len(result["clusters_searched"]) == 6
    assert gauge.peak == tools._CLUSTER_SEARCH_CONCURRENCY


def test_a_match_does_not_wait_for_clusters_that_hang(clusters):
    clusters(c0=_Cluster(block=True), c1=_Cluster(block=True), c2=_Cluster(20))

    async def run():
        return await asyncio.wait_for(
            tools.query_babylon_catalog("list_anarchy_subjects", guid="g00007"), timeout=5
        )

    result = asyncio.run(run())
    assert result["cluster"] == "c2" and result["count"] == 1


def test_capped_anarchy_action_listing_is_not_a_clean_miss(clusters, monkeypatch):
    monkeypatch.setattr(tools, "_ACTION_SCAN_MAX_ITEMS", 30)

    def action(i: int) -> dict:
        return {"metadata": {"name": f"a{i}", "namespace": _NS}, "spec": {}, "status": {}}

    clusters(page_size=10, east=_Cluster(100, make=action))
    result = asyncio.run(tools.query_babylon_catalog("list_anarchy_actions", guid="nomatch"))
    assert result["incomplete"] is True and result["partially_searched_clusters"] == ["east"]
