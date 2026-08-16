"""Dependency container — one place that decides local vs Azure.

Every collaborator the pipeline and API need is assembled here and hung off
`app.state.deps`. Route handlers and jobs read from it; nothing constructs a
client inline. That keeps the local/Azure choice to a single readable function
instead of scattering `if settings.environment == "local"` through the codebase.

Selection is by `ENVIRONMENT`, not by which packages happen to be installed —
a missing `azure-*` dependency in a deployed revision should fail loudly at
startup, not silently downgrade to in-memory storage that drops every write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.config import Settings
from app.config import settings as default_settings
from app.data.ports import (
    AuditLog,
    BlobStore,
    ClauseRepository,
    ContractRepository,
    IngestQueue,
    Notifier,
    ObligationRepository,
)
from app.ingest.doc_intelligence import LayoutAnalyzer
from app.ingest.embeddings import Embedder
from app.ingest.extractor import ObligationModel

logger = logging.getLogger(__name__)


@runtime_checkable
class Closeable(Protocol):
    async def aclose(self) -> None: ...


@dataclass(slots=True)
class Dependencies:
    contracts: ContractRepository
    clauses: ClauseRepository
    blobs: BlobStore
    queue: IngestQueue
    audit: AuditLog
    analyzer: LayoutAnalyzer
    obligations: ObligationRepository | None = None
    # None until a Foundry resource is configured; run_ingest then produces
    # clauses only, rather than failing.
    model: ObligationModel | None = None
    # None disables search; the ingest path still stores clauses without
    # vectors, which is a coherent (if less useful) state to run in.
    embedder: Embedder | None = None
    # Where the daily scan reports. Defaults to logging, which is visible in
    # the container log and reaches nobody.
    notifier: Notifier | None = None
    # Adapters holding a credential or an HTTP pool. Empty for the local
    # adapters, which own nothing that needs releasing.
    closers: list[Closeable] = field(default_factory=list)

    async def aclose(self) -> None:
        """Release every adapter, continuing past failures.

        One adapter refusing to close must not strand the others — a leaked
        credential and its token-refresh task outlive the process's usefulness.
        """
        for closer in self.closers:
            try:
                await closer.aclose()
            except Exception:
                logger.exception("failed to close %s", type(closer).__name__)


def build_local_dependencies(analyzer: LayoutAnalyzer | None = None) -> Dependencies:
    """In-memory repositories, filesystem blobs, pypdf text extraction.

    Document Intelligence has no offline equivalent, so the analyzer falls back
    to `LocalTextAnalyzer` — text only, no bounding boxes, no roles, no table
    structure. Good enough to exercise the pipeline end to end on a laptop, and
    explicitly not good enough to judge chunk quality by; see that module's
    docstring for what it loses.

    If pypdf isn't installed the fallback raises on use rather than at import,
    so an unconfigured analyzer fails with an actionable message instead of
    blocking startup.
    """
    from app.data.local import (
        InMemoryAuditLog,
        InMemoryClauseRepository,
        InMemoryContractRepository,
        InMemoryIngestQueue,
        InMemoryObligationRepository,
        LocalBlobStore,
    )
    from app.ingest.embeddings import HashingEmbedder
    from app.notify import LoggingNotifier

    if analyzer is None:
        try:
            from app.ingest.local_analyzer import LocalTextAnalyzer

            analyzer = LocalTextAnalyzer()
            logger.warning(
                "using LocalTextAnalyzer: no bounding boxes, no roles, no table "
                "structure. Development only."
            )
        except RuntimeError:
            analyzer = _UnconfiguredAnalyzer()

    return Dependencies(
        contracts=InMemoryContractRepository(),
        clauses=InMemoryClauseRepository(),
        blobs=LocalBlobStore(),
        queue=InMemoryIngestQueue(),
        audit=InMemoryAuditLog(),
        analyzer=analyzer,
        obligations=InMemoryObligationRepository(),
        # No local Claude. Extraction is skipped unless a caller injects a
        # model, so the local loop still runs end to end and stops at clauses.
        model=None,
        # Search, unlike extraction, does work locally: HashingEmbedder gives
        # real lexical similarity with no model behind it.
        embedder=HashingEmbedder(),
        notifier=LoggingNotifier(),
    )


def build_azure_dependencies(settings: Settings) -> Dependencies:
    """Real adapters. Imported lazily so the local path never needs the SDKs."""

    missing = [
        name
        for name, value in (
            ("COSMOS_ENDPOINT", settings.cosmos_endpoint),
            ("STORAGE_ACCOUNT_URL", settings.storage_account_url),
            ("SERVICEBUS_NAMESPACE", settings.servicebus_namespace),
            ("DOC_INTELLIGENCE_ENDPOINT", settings.doc_intelligence_endpoint),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"ENVIRONMENT={settings.environment} requires these settings: {', '.join(missing)}"
        )

    from app.data.azure_blob import AzureBlobStore
    from app.data.azure_cosmos import (
        CosmosAuditLog,
        CosmosClauseRepository,
        CosmosConnection,
        CosmosContractRepository,
        CosmosObligationRepository,
    )
    from app.data.azure_queue import AzureServiceBusQueue
    from app.ingest.doc_intelligence import AzureDocumentIntelligenceAnalyzer
    from app.ingest.embeddings import AzureOpenAIEmbedder
    from app.ingest.extractor import FoundryObligationModel
    from app.notify import LoggingNotifier, WebhookNotifier

    # One connection shared by all three Cosmos repositories, so a process
    # holds one client rather than three.
    cosmos = CosmosConnection(settings.cosmos_endpoint, settings.cosmos_database)
    blobs = AzureBlobStore(settings.storage_account_url, settings.contracts_container)
    queue = AzureServiceBusQueue(settings.servicebus_namespace, settings.ingest_queue)
    analyzer = AzureDocumentIntelligenceAnalyzer(
        settings.doc_intelligence_endpoint, settings.doc_intelligence_model
    )

    # Foundry is optional: without it the deployed pipeline still ingests and
    # chunks, which is a useful state to be able to run.
    model = None
    if settings.foundry_resource:
        model = FoundryObligationModel(
            settings.foundry_resource,
            settings.extraction_model,
            max_tokens=settings.extraction_max_tokens,
            api_key=settings.foundry_api_key,
        )
    else:
        logger.warning(
            "FOUNDRY_RESOURCE is unset - ingest will produce clauses but no obligations"
        )

    embedder = None
    if settings.azure_openai_endpoint:
        embedder = AzureOpenAIEmbedder(
            settings.azure_openai_endpoint,
            settings.embedding_deployment,
            dimensions=settings.embedding_dimensions,
        )
    else:
        logger.warning(
            "AZURE_OPENAI_ENDPOINT is unset - clauses will be stored without vectors "
            "and search will return 503"
        )

    notifier = (
        WebhookNotifier(settings.notify_webhook_url)
        if settings.notify_webhook_url
        else LoggingNotifier()
    )

    return Dependencies(
        contracts=CosmosContractRepository(cosmos),
        clauses=CosmosClauseRepository(cosmos),
        blobs=blobs,
        queue=queue,
        audit=CosmosAuditLog(cosmos),
        analyzer=analyzer,
        obligations=CosmosObligationRepository(cosmos),
        model=model,
        embedder=embedder,
        notifier=notifier,
        # Each of these holds a credential and an HTTP pool. Without explicit
        # teardown the process leaks them, along with the credential's
        # background token-refresh task.
        closers=(
            [cosmos, blobs, queue, analyzer]
            + ([model] if model else [])
            + ([embedder] if embedder else [])
        ),
    )


def build_dependencies(
    settings: Settings | None = None, *, analyzer: LayoutAnalyzer | None = None
) -> Dependencies:
    settings = settings or default_settings
    if settings.environment == "local":
        logger.info("using local dependencies (in-memory repositories, filesystem blobs)")
        return build_local_dependencies(analyzer)
    return build_azure_dependencies(settings)


class _UnconfiguredAnalyzer:
    """Placeholder that fails with an actionable message instead of a
    None-dereference three frames deep in the pipeline."""

    async def analyze(self, pdf_bytes: bytes):
        raise RuntimeError(
            "No layout analyzer configured. Document Intelligence has no local "
            "equivalent — either point ENVIRONMENT at Azure, or inject a "
            "StaticLayoutAnalyzer (see tests/conftest.py)."
        )
