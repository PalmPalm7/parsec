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
