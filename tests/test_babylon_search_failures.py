"""A Babylon cluster that could not be searched must not read as "not found".

On the live pods the credentials for five Babylon clusters were rejected (401)
and babydev no longer resolved. The all-cluster GUID search still returned
``{"subjects": [], "count": 0, "errors": [...]}`` with no ``error`` key (staging
q03, q04), and the Workshop search returned "Workshop '2w27z' not found on any
cluster ... Errors: []" (staging q11). The agents reported both as "not found",
and ToolStats counted them as successful calls.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import src.connections.babylon as conn
import src.tools.babylon as tools


class _Cluster:
    """A fake Babylon API server: healthy with ``items``, or failing with ``status``/DNS."""

    def __init__(self, items: list[dict] | None = None, status: int = 200, dns: bool = False):
        self.items = items or []
        self.status = status
        self.dns = dns

    async def get(self, path, params=None):
        request = httpx.Request("GET", f"https://api.test{path}")
        if self.dns:
            raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)
        if self.status != 200:
            return httpx.Response(self.status, request=request, json={"kind": "Status"})
        items = self.items
        selector = dict(params or {}).get("fieldSelector", "")
        if selector:
            items = [i for i in items if i["metadata"]["name"] == selector.split("=", 1)[1]]
        return httpx.Response(200, request=request, json={"items": items, "metadata": {}})


@pytest.fixture
def clusters(monkeypatch):
    def install(**by_name: _Cluster) -> None:
        async def _get_client(name):
            return by_name[name]

        monkeypatch.setattr(conn, "_get_client", _get_client)
        monkeypatch.setattr(tools, "get_configured_clusters", lambda: list(by_name))

    return install


def _subjects(guid: str) -> dict:
    return asyncio.run(tools.query_babylon_catalog("list_anarchy_subjects", guid=guid))


def test_guid_search_with_every_cluster_failing_is_an_error(clusters):
    clusters(
        east=_Cluster(status=401),
        west=_Cluster(status=401),
        babydev=_Cluster(dns=True),
    )
    result = _subjects("2w27z")
    assert "error" in result and "subjects" not in result
    assert "not a 'not found'" in result["error"]
    assert len(result["errors"]) == 3
    assert any("401" in e for e in result["errors"])
    assert any("Name or service not known" in e for e in result["errors"])


def test_guid_search_with_some_clusters_failing_is_marked_incomplete(clusters):
    clusters(east=_Cluster(status=401), prod=_Cluster(items=[]), babydev=_Cluster(dns=True))
    result = _subjects("2w27z")
    assert "error" not in result
    assert result["count"] == 0
    assert result["incomplete"] is True
    assert result["unsearched_clusters"] == ["east", "babydev"]
    assert result["clusters_searched"] == ["prod"]


def test_guid_search_that_searched_everything_is_a_clean_miss(clusters):
    clusters(east=_Cluster(items=[]), prod=_Cluster(items=[]))
    result = _subjects("2w27z")
    assert "error" not in result and "incomplete" not in result
    assert result["count"] == 0 and result["errors"] is None


def test_single_cluster_listing_that_failed_is_an_error(clusters):
    clusters(east=_Cluster(status=401))
    result = asyncio.run(
        tools.query_babylon_catalog("list_anarchy_subjects", cluster="east", search="x")
    )
    assert "error" in result and "401" in result["error"]


def test_anarchy_action_guid_search_with_every_cluster_failing_is_an_error(clusters):
    clusters(east=_Cluster(status=401), west=_Cluster(status=403))
    result = asyncio.run(tools.query_babylon_catalog("list_anarchy_actions", guid="2w27z"))
    assert "error" in result and "actions" not in result


@pytest.mark.parametrize("action", ["get_workshop", "get_multiworkshop"])
def test_name_search_with_every_cluster_failing_does_not_say_not_found(clusters, action):
    clusters(east=_Cluster(status=401), west=_Cluster(status=401), babydev=_Cluster(dns=True))
    result = asyncio.run(tools.query_babylon_catalog(action, name="2w27z"))
    assert "not found on any cluster" not in result["error"]
    assert "Could not search 3/3 Babylon clusters" in result["error"]
    assert "401" in result["error"]


@pytest.mark.parametrize("action", ["get_workshop", "get_multiworkshop"])
def test_name_search_with_one_cluster_failing_does_not_say_not_found(clusters, action):
    clusters(east=_Cluster(items=[]), west=_Cluster(status=401))
    result = asyncio.run(tools.query_babylon_catalog(action, name="2w27z"))
    assert "not found on any cluster" not in result["error"]
    assert "Could not search 1/2 Babylon clusters" in result["error"]


@pytest.mark.parametrize("action", ["get_workshop", "get_multiworkshop"])
def test_name_search_that_searched_everything_says_not_found(clusters, action):
    # west has no such resource type at all (404 on the list): that is also absence.
    clusters(east=_Cluster(items=[]), west=_Cluster(status=404))
    result = asyncio.run(tools.query_babylon_catalog(action, name="2w27z"))
    assert "not found on any cluster" in result["error"]
