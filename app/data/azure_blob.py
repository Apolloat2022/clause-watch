"""Blob Storage adapter — the immutable raw-PDF store.

Authenticates with DefaultAzureCredential (the user-assigned Managed Identity
in Container Apps, `az login` locally). The storage account has shared-key auth
disabled in Bicep, so there is no connection string that could work even if one
leaked.
"""

from __future__ import annotations

import logging

from azure.core.exceptions import ResourceExistsError
from azure.identity.aio import DefaultAzureCredential
from azure.storage.blob.aio import BlobServiceClient

logger = logging.getLogger(__name__)


class AzureBlobStore:
    """Write-once blob storage.

    `put` refuses to overwrite. Contracts are the immutable input the whole
    pipeline replays from — silently replacing one would make every previously
    extracted clause and obligation unverifiable against its source.
    """

    def __init__(self, account_url: str, container: str = "contracts"):
        self._credential = DefaultAzureCredential()
        self._service = BlobServiceClient(account_url=account_url, credential=self._credential)
        self._container = container

    async def put(self, name: str, data: bytes) -> str:
        blob = self._service.get_blob_client(container=self._container, blob=name)
        try:
            # overwrite=False turns a duplicate into ResourceExistsError rather
            # than a silent replacement.
            await blob.upload_blob(data, overwrite=False)
        except ResourceExistsError as exc:
            raise FileExistsError(f"blob already exists: {name}") from exc
        return blob.url

    async def get(self, uri: str) -> bytes:
        blob = self._blob_client_for(uri)
        stream = await blob.download_blob()
        return await stream.readall()

    def _blob_client_for(self, uri: str):
        """Resolve a stored blob URI back to a client.

        Blob names contain a '/' (they are namespaced `{contract_id}/{file}`),
        so the container is taken as the first path segment and everything
        after it is the blob name — splitting on the last '/' would break.
        """
        from urllib.parse import unquote, urlparse

        path = unquote(urlparse(uri).path).lstrip("/")
        container, _, blob_name = path.partition("/")
        if not blob_name:
            raise ValueError(f"blob uri has no blob name: {uri}")
        return self._service.get_blob_client(container=container, blob=blob_name)

    async def aclose(self) -> None:
        await self._service.close()
        await self._credential.close()
