"""GCP billing clients authenticate as gcp.credentials_path, not the ambient ADC.

On the staging pod GOOGLE_APPLICATION_CREDENTIALS pointed at the Vertex service
account, and init_gcp only setdefault()-ed the billing path over it, so BigQuery ran
as the Vertex SA and got "BigQuery API has not been used in project 346881159400".
These tests build real clients from two throwaway service-account files and check
which identity each client ended up with; nothing is sent over the network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.connections import gcp
from src.tools import gcp_projects

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


def test_no_credentials_path_falls_back_to_adc(vertex_ambient_billing_configured):
    # Local development runs on gcloud ADC with no service-account file configured.
    del vertex_ambient_billing_configured["gcp_cfg"]["credentials_path"]

    assert gcp.get_gcp_credentials() is None
    client = gcp_projects._get_projects_client()
    assert client._transport._credentials.service_account_email == VERTEX_SA
