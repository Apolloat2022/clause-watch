"""Offline checks on the Azure adapters.

These make no network calls. What they catch is the class of mistake that
otherwise only shows up at deploy time: an adapter that drifts out of shape
with its port, a missing transitive dependency, or a URI parser that works on
the happy path and breaks on the shape this system actually stores.

What they cannot catch is anything about how Azure actually responds. The
Document Intelligence response mapping and the Cosmos vector query both need an
integration test against a real subscription — see the note at the bottom.
"""

from __future__ import annotations

import pytest

from app.data.azure_blob import AzureBlobStore
from app.data.azure_cosmos import (
    CosmosAuditLog,
    CosmosClauseRepository,
    CosmosConnection,
    CosmosContractRepository,
)
from app.data.azure_queue import AzureServiceBusQueue
from app.data.ports import AuditLog, BlobStore, ClauseRepository, ContractRepository, IngestQueue

BLOB_URL = "https://example.blob.core.windows.net/"
COSMOS_URL = "https://example.documents.azure.com:443/"
SERVICEBUS_NS = "example.servicebus.windows.net"


@pytest.fixture
async def cosmos():
    connection = CosmosConnection(COSMOS_URL, "clausewatch")
    yield connection
    await connection.aclose()


@pytest.fixture
async def blobs():
    store = AzureBlobStore(BLOB_URL, "contracts")
    yield store
    await store.aclose()


@pytest.fixture
async def queue():
    q = AzureServiceBusQueue(SERVICEBUS_NS, "contract-ingest")
    yield q
    await q.aclose()


async def test_adapters_satisfy_their_ports(cosmos, blobs, queue):
    # Constructing at all proves the dependency closure is complete —
    # azure-identity's async credentials need aiohttp, which none of the azure
    # packages declare, and its absence raises only when a pipeline is built.
    assert isinstance(blobs, BlobStore)
    assert isinstance(queue, IngestQueue)
    assert isinstance(CosmosContractRepository(cosmos), ContractRepository)
    assert isinstance(CosmosClauseRepository(cosmos), ClauseRepository)
    assert isinstance(CosmosAuditLog(cosmos), AuditLog)


async def test_blob_uri_resolves_a_namespaced_name(blobs):
    # Blob names are namespaced `{contract_id}/{filename}`, so they contain a
    # slash. Splitting the URI path on the *last* slash would yield "msa.pdf"
    # and a container of "contracts/abc-123" — a 404 at read time, long after
    # the write appeared to succeed.
    client = blobs._blob_client_for(f"{BLOB_URL}contracts/abc-123/msa.pdf")
    assert client.container_name == "contracts"
    assert client.blob_name == "abc-123/msa.pdf"


async def test_blob_uri_with_url_encoded_characters(blobs):
    client = blobs._blob_client_for(f"{BLOB_URL}contracts/abc-123/Master%20Agreement.pdf")
    assert client.blob_name == "abc-123/Master Agreement.pdf"


async def test_blob_uri_without_a_blob_name_is_rejected(blobs):
    with pytest.raises(ValueError, match="no blob name"):
        blobs._blob_client_for(f"{BLOB_URL}contracts")


async def test_dependencies_close_even_when_one_adapter_fails():
    # A single misbehaving adapter must not strand the rest; a leaked
    # credential keeps a token-refresh task alive past process usefulness.
    from app.deps import Dependencies

    closed: list[str] = []

    class Exploding:
        async def aclose(self):
            raise RuntimeError("boom")

    class Recording:
        async def aclose(self):
            closed.append("ok")

    deps = Dependencies(
        contracts=None,
        clauses=None,
        blobs=None,
        queue=None,
        audit=None,
        analyzer=None,
        closers=[Exploding(), Recording()],
    )
    await deps.aclose()

    assert closed == ["ok"]


# Not covered here, and it matters:
#   * AzureDocumentIntelligenceAnalyzer._to_layout maps a real analyze result.
#     It is written from the SDK's documented shape but has never run against
#     one, so paragraph roles, polygon units, and the paragraph/table interleave
#     are all unverified.
#   * The Cosmos vector query (phase 4) — VectorDistance() has no emulator.
# Both need an integration test against a live subscription.
