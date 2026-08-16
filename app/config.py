"""All runtime configuration, sourced from the environment.

No connection strings for Azure services: every resource is reached with
DefaultAzureCredential (Managed Identity in Container Apps, developer
credentials locally), so what lives here are endpoints and names, not secrets.
The one unavoidable key is the Foundry one — see `foundry_api_key`.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Cosmos DB -------------------------------------------------------
    cosmos_endpoint: str = ""
    cosmos_database: str = "clausewatch"

    # --- Blob Storage ----------------------------------------------------
    storage_account_url: str = ""
    contracts_container: str = "contracts"

    # --- Service Bus -----------------------------------------------------
    servicebus_namespace: str = ""
    ingest_queue: str = "contract-ingest"

    # --- Document Intelligence -------------------------------------------
    doc_intelligence_endpoint: str = ""
    # "prebuilt-layout" gives structure (tables, lines, bounding boxes) without
    # a trained model. "prebuilt-contract" adds contract-specific fields but is
    # a different price point — start with layout, measure, then decide.
    doc_intelligence_model: str = "prebuilt-layout"

    # --- Foundry (Claude for extraction, Azure OpenAI for embeddings) -----
    foundry_resource: str = ""
    # Optional. The Foundry client prefers Managed Identity via
    # azure_ad_token_provider, matching every other Azure client here; a key is
    # only needed where a token credential is not available. If set, keep it in
    # Key Vault and inject it as a container secret.
    foundry_api_key: str | None = None
    extraction_model: str = "claude-opus-5"
    # Caps thinking and response text together on current models, so a tight
    # budget truncates the JSON body rather than the reasoning.
    extraction_max_tokens: int = 16_000
    # Azure OpenAI endpoint for the embedding deployment. Usually the same
    # Foundry resource, addressed on its Azure OpenAI hostname rather than the
    # /anthropic path the Claude client uses.
    azure_openai_endpoint: str = ""
    embedding_deployment: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    # Bumped whenever the extraction prompt changes, and stored on every
    # obligation — this is what makes a bad batch findable and re-runnable.
    prompt_version: str = "v1"

    # --- Auth ------------------------------------------------------------
    # Entra ID JWT validation. Unset tenant/audience disables enforcement so
    # local runs and tests stay frictionless; app/main.py logs a warning when
    # that is the case. Any deployed revision must set both.
    entra_tenant_id: str | None = None
    entra_audience: str | None = None

    # --- Behavior --------------------------------------------------------
    # Obligations closer than this are flagged DUE_SOON by the scanner job.
    due_soon_days: int = 30
    # Extractions below this are stored but withheld from default listings;
    # deliberately not a hard reject until the score is calibrated (see
    # ARCHITECTURE.md §8).
    min_confidence: float = 0.35

    # Incoming-webhook URL (Teams / Slack) for daily scan summaries. Unset
    # falls back to logging only, which is visible in the container log and
    # reaches nobody.
    notify_webhook_url: str | None = None

    log_level: str = "INFO"
    environment: Literal["local", "dev", "prod"] = "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
