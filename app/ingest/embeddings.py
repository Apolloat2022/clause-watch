"""Clause text -> vectors.

Two implementations behind one port. `AzureOpenAIEmbedder` calls the
`text-embedding-3-small` deployment in the Foundry resource — separate from
extractor.py because Anthropic serves no embedding model, so the split is
forced rather than stylistic. `HashingEmbedder` is a local, model-free
stand-in that produces real lexical similarity, which is what makes vector
search testable offline.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+")

# text-embedding-3-small's cap is 2048 inputs per request; well under it keeps
# a single oversized clause from pushing a batch past the token limit too.
EMBED_BATCH_SIZE = 256


@runtime_checkable
class Embedder(Protocol):
    @property
    def dimensions(self) -> int: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, tolerant of unnormalized input.

    Both embedders return L2-normalized vectors, so this reduces to a dot
    product in practice — but normalizing here as well means a vector from
    somewhere else can't silently skew a ranking.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class HashingEmbedder:
    """Token- and character-n-gram hashing — deterministic, offline, lexical.

    Feature hashing with a sign bit, in the manner of scikit-learn's
    HashingVectorizer, over whole tokens *plus* the character n-grams inside
    them. The n-grams are what make it useful rather than merely mechanical:
    whole-token hashing alone treats "invoice" and "invoices" as unrelated, so
    an obviously-relevant clause scores zero and ranking falls back to document
    order. With subword features, morphological variants match and the search
    endpoint can be tested for behavior.

    **It is still not semantic.** "remuneration" and "payment" share no
    characters and are unrelated to it. Treat local results as verifying
    plumbing and ranking, never as an indication of what Azure OpenAI returns.

    Hashing uses blake2b rather than the builtin `hash()`: Python randomizes
    string hashing per process, so a builtin-hash implementation would make a
    vector stored by the worker incomparable to a query vector computed by the
    API — search would silently return noise.
    """

    NGRAM = 4

    def __init__(self, dimensions: int = 1536):
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def _features(self, text: str):
        for token in _TOKEN.findall(text.lower()):
            yield token
            for start in range(len(token) - self.NGRAM + 1):
                yield token[start : start + self.NGRAM]

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            bucket = value % self._dimensions
            # Sign bit spreads collisions in both directions so they cancel on
            # average rather than always inflating a bucket.
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vector[bucket] += sign

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]


class AzureOpenAIEmbedder:
    """`text-embedding-3-small` on the Foundry resource, via Managed Identity."""

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        *,
        dimensions: int = 1536,
        api_version: str = "2024-10-21",
    ):
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
        from openai import AsyncAzureOpenAI

        self._dimensions = dimensions
        self._deployment = deployment
        self._credential = DefaultAzureCredential()
        self._client = AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_version=api_version,
            azure_ad_token_provider=get_bearer_token_provider(
                self._credential, "https://cognitiveservices.azure.com/.default"
            ),
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batched, because per-clause calls on a 60-page contract are slow and
        wasteful. Order is preserved by sorting on the response index rather
        than trusting arrival order."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            chunk = texts[start : start + EMBED_BATCH_SIZE]
            response = await self._client.embeddings.create(
                model=self._deployment,
                input=chunk,
                # text-embedding-3-* supports shortening the vector; it must
                # match the Cosmos container's vector policy exactly or the
                # index rejects the write.
                dimensions=self._dimensions,
            )
            vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
        return vectors

    async def aclose(self) -> None:
        await self._client.close()
        await self._credential.close()
