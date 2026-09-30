"""GCP BigQuery client for billing queries."""

import logging

from src.config import get_config

logger = logging.getLogger(__name__)

_bq_client = None


def get_gcp_credentials():
    """Credentials from gcp.credentials_path, or None to use Application Default Credentials.

    The file is loaded explicitly rather than exported as GOOGLE_APPLICATION_CREDENTIALS:
    on a Vertex deployment that variable already names the Vertex service account, so
    the billing clients silently ran as the wrong identity.
    """
    creds_path = get_config().gcp.get("credentials_path", "")
    if not creds_path:
        return None

    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(creds_path)


def init_gcp() -> None:
    """Initialize the BigQuery client."""
    global _bq_client
    cfg = get_config()
    gcp_cfg = cfg.gcp

    project_id = gcp_cfg.get("project_id", "")
    if not project_id:
        logger.warning("GCP project_id not configured — GCP tools disabled")
        return

    credentials = get_gcp_credentials()

    from google.cloud import bigquery

    _bq_client = bigquery.Client(project=project_id, credentials=credentials)
    logger.info(
        "GCP BigQuery client initialized (project=%s, credentials=%s)",
        project_id,
        gcp_cfg.get("credentials_path", "") or "ADC",
    )


def get_bq_client():
    """Get the BigQuery client (None if not configured)."""
    return _bq_client
