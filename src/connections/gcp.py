"""GCP BigQuery client for billing queries."""

import logging

from src.config import get_config

logger = logging.getLogger(__name__)

_bq_client = None

# Both billing clients (BigQuery, Resource Manager) accept this scope.
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def get_gcp_credentials():
    """Credentials from gcp.credentials_path, or None to use Application Default Credentials.

    The file is loaded explicitly rather than exported as GOOGLE_APPLICATION_CREDENTIALS:
    on a Vertex deployment that variable already names the Vertex service account, so
    the billing clients silently ran as the wrong identity.

    load_credentials_from_file reads the same file types ADC does (service_account,
    authorized_user, external_account, impersonated_service_account). Before the
    explicit load, credentials_path went through ADC, so a gcloud user file or a
    Workload Identity Federation config was accepted; a service-account-only loader
    would reject those with MalformedError.
    """
    creds_path = get_config().gcp.get("credentials_path", "")
    if not creds_path:
        return None

    import google.auth

    credentials, _project = google.auth.load_credentials_from_file(
        creds_path, scopes=[_CLOUD_PLATFORM_SCOPE]
    )
    return credentials


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
