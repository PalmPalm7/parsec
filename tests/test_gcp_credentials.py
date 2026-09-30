"""GCP billing clients authenticate as gcp.credentials_path, not the ambient ADC.

On the staging pod GOOGLE_APPLICATION_CREDENTIALS pointed at the Vertex service
account, and init_gcp only setdefault()-ed the billing path over it, so BigQuery ran
as the Vertex SA and got "BigQuery API has not been used in project 346881159400".
These tests build real clients from two throwaway service-account files and check
which identity each client ended up with; nothing is sent over the network.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth.exceptions import DefaultCredentialsError

from src.connections import gcp
from src.tools import gcp_costs, gcp_projects

VERTEX_SA = "vertex@parsec-test.iam.gserviceaccount.com"
BILLING_SA = "billing@parsec-test.iam.gserviceaccount.com"


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _write_service_account(path: Path, email: str, pem: str) -> str:
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "parsec-test",
                "private_key_id": "test-key",
                "private_key": pem,
                "client_email": email,
                "client_id": "1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )
    return str(path)


@pytest.fixture
def vertex_ambient_billing_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, private_key_pem: str
) -> dict:
    """The staging pod: ADC names the Vertex SA, gcp.credentials_path the billing SA."""
    vertex = _write_service_account(tmp_path / "vertex.json", VERTEX_SA, private_key_pem)
    billing = _write_service_account(tmp_path / "billing.json", BILLING_SA, private_key_pem)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", vertex)
    gcp_cfg = {"project_id": "billing-project", "credentials_path": billing}
    monkeypatch.setattr(gcp, "get_config", lambda: SimpleNamespace(gcp=gcp_cfg))
    monkeypatch.setattr(gcp, "_bq_client", None)
    monkeypatch.setattr(gcp, "_init_error", None)
    return {"vertex": vertex, "billing": billing, "gcp_cfg": gcp_cfg}


def test_bigquery_client_uses_configured_service_account(vertex_ambient_billing_configured):
    gcp.init_gcp()

    client = gcp.get_bq_client()
    assert client is not None
    assert client._credentials.service_account_email == BILLING_SA


def test_projects_client_uses_configured_service_account(vertex_ambient_billing_configured):
    client = gcp_projects._get_projects_client()

    assert client._transport._credentials.service_account_email == BILLING_SA


def test_init_leaves_google_application_credentials_alone(
    vertex_ambient_billing_configured, monkeypatch: pytest.MonkeyPatch
):
    # The process-wide variable also reaches the Claude CLI subprocess and any other
    # ADC consumer, so configuring the billing SA must not rewrite it.
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")

    gcp.init_gcp()

    assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ


def test_authorized_user_file_is_accepted(vertex_ambient_billing_configured, tmp_path: Path):
    # gcp.credentials_path used to go through ADC, which takes a gcloud user file
    # (and WIF configs) as well as SA keys; a service-account-only loader raised
    # MalformedError on it and took GCP down.
    user_file = tmp_path / "adc-user.json"
    user_file.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "billing-client.apps.googleusercontent.com",
                "client_secret": "not-a-secret",
                "refresh_token": "billing-refresh-token",
            }
        )
    )
    vertex_ambient_billing_configured["gcp_cfg"]["credentials_path"] = str(user_file)

    gcp.init_gcp()

    client = gcp.get_bq_client()
    assert client is not None
    assert client._credentials.refresh_token == "billing-refresh-token"
    projects = gcp_projects._get_projects_client()
    assert projects._transport._credentials.refresh_token == "billing-refresh-token"


def test_no_credentials_path_falls_back_to_adc(vertex_ambient_billing_configured):
    # Local development runs on gcloud ADC with no service-account file configured.
    del vertex_ambient_billing_configured["gcp_cfg"]["credentials_path"]

    assert gcp.get_gcp_credentials() is None
    client = gcp_projects._get_projects_client()
    assert client._transport._credentials.service_account_email == VERTEX_SA


def test_costs_tool_reports_why_init_failed(vertex_ambient_billing_configured, tmp_path: Path):
    # The billing secret volume is optional in manifests.yaml.j2: with it unmounted,
    # init fails, app.py logs it and moves on, and the tool used to answer "not
    # configured" — which sends the reader to the config, not to the missing mount.
    missing = tmp_path / "not-mounted" / "service-account.json"
    vertex_ambient_billing_configured["gcp_cfg"]["credentials_path"] = str(missing)

    with pytest.raises(DefaultCredentialsError):
        gcp.init_gcp()

    result = asyncio.run(gcp_costs.query_gcp_costs("2026-09-01", "2026-09-30"))
    assert result["error"].startswith("GCP BigQuery failed to initialize: ")
    assert str(missing) in result["error"]


def test_costs_tool_still_says_not_configured_without_a_project(
    vertex_ambient_billing_configured,
):
    vertex_ambient_billing_configured["gcp_cfg"]["project_id"] = ""

    gcp.init_gcp()

    result = asyncio.run(gcp_costs.query_gcp_costs("2026-09-01", "2026-09-30"))
    assert result == {"error": "GCP BigQuery not configured"}
