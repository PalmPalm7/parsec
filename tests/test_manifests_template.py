"""Render playbooks/templates/manifests.yaml.j2 the way the deploy playbook does.

The ConfigMap in this template replaces config/config.yaml wholesale in the
pod, so a key the template does not render never reaches a deployment,
whatever config.yaml says. These tests render the template with
playbooks/vars/common.yml plus a minimal env file and parse the result.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
import yaml

jinja2 = pytest.importorskip("jinja2")

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAKE_ENV = {
    "env": "dev",
    "target_namespace": "parsec-dev",
    "cluster_domain": "apps.example.com",
    "git_branch": "main",
}


def _ansible_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "on", "true", "y", "yes"}


def _common_vars() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "playbooks/vars/common.yml").read_text())


def render(**overrides: Any) -> list[dict]:
    """Render with common.yml + FAKE_ENV + overrides; return the YAML documents.

    A None override removes the variable, as if no vars file set it.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT / "playbooks/templates")),
        trim_blocks=True,  # the ansible template module's default
        undefined=jinja2.StrictUndefined,
    )
    env.filters["to_json"] = json.dumps
    env.filters["bool"] = _ansible_bool
    variables = {**_common_vars(), **FAKE_ENV, **overrides}
    variables = {k: v for k, v in variables.items() if v is not None}
    text = env.get_template("manifests.yaml.j2").render(**variables)
    return [doc for doc in yaml.safe_load_all(text) if doc]


def _app_config(docs: list[dict]) -> dict:
    (cm,) = [
        d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "parsec-config"
    ]
    return yaml.safe_load(cm["data"]["config.yaml"])


class TestSdkTurnBudgets:
    def test_common_vars_render_the_turn_budgets(self):
        sdk = _app_config(render())["agent"]["sdk"]

        assert sdk["max_turns"] == 30
        assert sdk["subagent_min_turns"] == 20
        assert sdk["turn_timeout"] == 900

    def test_template_defaults_without_the_vars(self):
        docs = render(sdk_max_turns=None, sdk_subagent_min_turns=None, sdk_turn_timeout=None)
        sdk = _app_config(docs)["agent"]["sdk"]

        assert (sdk["max_turns"], sdk["subagent_min_turns"], sdk["turn_timeout"]) == (30, 20, 900)

    def test_env_file_overrides(self):
        docs = render(sdk_max_turns=40, sdk_subagent_min_turns=25, sdk_turn_timeout=0)
        sdk = _app_config(docs)["agent"]["sdk"]

        assert (sdk["max_turns"], sdk["subagent_min_turns"], sdk["turn_timeout"]) == (40, 25, 0)

    def test_existing_sdk_keys_are_unchanged(self):
        sdk = _app_config(render())["agent"]["sdk"]

        assert sdk["enabled_agents"] == ["all"]
        assert sdk["orchestrator"] is True
        assert sdk["allow_writes"] is False
        assert sdk["timeout"] == 300


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def _deployment(docs: list[dict], name: str) -> dict:
    (dep,) = [d for d in _by_kind(docs, "Deployment") if d["metadata"]["name"] == name]
    return dep


class TestNetworkPolicy:
    """Found on both deployments: no NetworkPolicy, and every Service targets port 8000.

    The app trusts X-Forwarded-Email, which only the oauth-proxy sets after a
    login, so any in-cluster caller could reach parsec-service and claim to be
    any user (report F14). The policy is opt-in until verified on a cluster.
    """

    def test_off_by_default(self):
        assert _by_kind(render(), "NetworkPolicy") == []
        assert _by_kind(render(network_policy_enabled=None), "NetworkPolicy") == []

    @pytest.mark.parametrize("flag", [True, "true", "yes"])
    def test_enabled_renders_one_policy(self, flag):
        assert len(_by_kind(render(network_policy_enabled=flag), "NetworkPolicy")) == 1

    def test_policy_admits_only_proxy_router_and_host_network_on_the_app_port(self):
        docs = render(network_policy_enabled=True)
        (policy,) = _by_kind(docs, "NetworkPolicy")
        spec = policy["spec"]
        app = _deployment(docs, "parsec")["spec"]["template"]
        proxy = _deployment(docs, "oauth-proxy")["spec"]["template"]
        (container,) = [c for c in app["spec"]["containers"] if c["name"] == "parsec"]

        # Selects the app pod, and only its ingress is restricted.
        assert spec["podSelector"]["matchLabels"].items() <= app["metadata"]["labels"].items()
        assert spec["policyTypes"] == ["Ingress"]

        (rule,) = spec["ingress"]
        assert rule["ports"] == [
            {"protocol": "TCP", "port": container["ports"][0]["containerPort"]}
        ]
        pod_peers = [p["podSelector"]["matchLabels"] for p in rule["from"] if "podSelector" in p]
        ns_peers = [
            p["namespaceSelector"]["matchLabels"] for p in rule["from"] if "namespaceSelector" in p
        ]
        assert len(pod_peers) + len(ns_peers) == len(rule["from"]) == 3
        # The one pod peer is the oauth-proxy's pod template, not the app itself.
        assert pod_peers == [proxy["metadata"]["labels"]]
        assert ns_peers == [
            {"policy-group.network.openshift.io/ingress": ""},
            {"policy-group.network.openshift.io/host-network": ""},
        ]
