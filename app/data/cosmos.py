"""Cosmos DB access: client construction and the four containers.

One client per process, built lazily from DefaultAzureCredential (Managed
Identity in Container Apps, developer credentials locally) - never a
connection string.

Containers, all partitioned by /contractId (see ARCHITECTURE.md section 4 for
the tradeoff this makes on cross-contract queries):

    contracts | clauses (vector-indexed) | obligations | audit
"""

from __future__ import annotations

# TODO(phase 2): async client + container handles.
# TODO(phase 4): vector query helper wrapping VectorDistance().
