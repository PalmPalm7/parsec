"""OCPV cluster-wide listings must be paged or namespaced, not one unbounded response.

The same shape of call that restarted parsec-dev on Babylon (an unpaged
cluster-wide list parsed on the event loop) existed for OCPV: list_pvs
fetched every PersistentVolume on the cluster in one response, and pods_top
fetched every pod's metrics cluster-wide only to keep one namespace.
"""

from __future__ import annotations

import pytest

import src.connections.ocpv as conn
import src.tools.ocpv as tools


class _Resp:
    def __init__(self, body: dict) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


class _PagedClient:
    """Serves `items` in pages of `limit`, with continue tokens; records each call."""

    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[tuple[str, dict]] = []

    async def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        start = int(params.get("continue") or 0)
        limit = int(params.get("limit") or len(self.items))
        page = self.items[start : start + limit]
        nxt = start + limit
        meta = {"continue": str(nxt)} if nxt < len(self.items) else {}
        return _Resp({"items": page, "metadata": meta})


def _pv(i: int) -> dict:
    return {
        "metadata": {"name": f"pvc-{i:05d}" if i % 100 else f"local-pv-{i:05d}"},
        "spec": {
            "storageClassName": "ocs-storagecluster-ceph-rbd",
            "capacity": {"storage": "10Gi"},
        },
        "status": {"phase": "Bound"},
    }


def _install(monkeypatch, items: list[dict]) -> _PagedClient:
    client = _PagedClient(items)

    async def _get_client(_cluster: str) -> _PagedClient:
        return client

    monkeypatch.setattr(conn, "_get_client", _get_client)
    return client


@pytest.mark.asyncio
async def test_list_pvs_reads_every_page_with_a_limit(monkeypatch):
    client = _install(monkeypatch, [_pv(i) for i in range(600)])

    result = await tools._list_pvs("ocpv07", "", 10)

    assert result["total_pvs"] == 600
    assert result["total_bound_gi"] == 6000
    assert [p["limit"] for _, p in client.calls] == [conn.LIST_PAGE_SIZE] * 3
    assert [p.get("continue") for _, p in client.calls] == [None, "250", "500"]
    assert {path for path, _ in client.calls} == {"/api/v1/persistentvolumes"}


@pytest.mark.asyncio
async def test_list_pvs_name_filter_applies_on_every_page(monkeypatch):
    _install(monkeypatch, [_pv(i) for i in range(600)])

    result = await tools._list_pvs("ocpv07", "LOCAL-PV", 10)

    # local-pv-00000, -00100, ... -00500: one per hundred, spread over all three pages
    assert result["total_pvs"] == 6
    assert result["total_bound_gi"] == 60


@pytest.mark.asyncio
async def test_pods_top_asks_for_one_namespace(monkeypatch):
    pod = {
        "metadata": {"name": "virt-launcher-vm1-abcde", "namespace": "sandbox-1"},
        "containers": [{"usage": {"cpu": "250m", "memory": "1048576Ki"}}],
    }
    client = _install(monkeypatch, [pod])

    result = await tools._pods_top("ocpv07", "sandbox-1", "", 10)

    assert [path for path, _ in client.calls] == [
        "/apis/metrics.k8s.io/v1beta1/namespaces/sandbox-1/pods"
    ]
    assert result["count"] == 1
    assert result["pods"][0]["name"] == "virt-launcher-vm1-abcde"
