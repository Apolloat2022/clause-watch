"""Blob Storage: the immutable raw-PDF store.

Uploads are write-once and never mutated, so an extraction can always be
re-run from the original bytes after a prompt or model change.
"""

from __future__ import annotations

# TODO(phase 2): upload_contract() / open_contract_stream().
