# ClauseWatch — Architecture

Contract obligation extraction and monitoring. Executed contracts go in; a
queryable, date-aware register of *who owes what, when, and under which clause*
comes out — with every obligation traceable to the page and region of the
source document it came from.

---

## 1. Why this shape

The hard problem in contract intelligence is not summarization, it is
**grounded structured extraction**. An obligation the system invents is worse
than one it misses: a missed renewal deadline is a known unknown, while a
hallucinated payment term is a confident lie that someone may act on.

Three decisions follow from that, and most of the design is downstream of them:

1. **Every obligation cites a clause span.** Extraction returns clause IDs, and
   a clause carries its page number and bounding box. An obligation that cannot
   be traced back to source text is rejected at write time, not surfaced with a
   caveat.
2. **Layout is signal, not noise.** Payment schedules, SLA tables, and
   termination matrices carry the numbers that matter. Flat text extraction
   loses the row/column relationships that make a table mean anything, which is
   why this uses Document Intelligence rather than a text-only parser.
3. **Extraction is deterministic-ish and replayable.** Documents are content
   hashed; re-ingesting the same bytes is a no-op. Every run records the model
   and prompt version that produced it, so a bad extraction can be found and
   re-run rather than silently persisting forever.

## 2. Flow

```
                  ┌──────────────┐
   PDF upload ───▶│  FastAPI     │──▶ Blob Storage (raw PDF, immutable)
                  │  (Container  │
                  │   App)       │──▶ Service Bus queue ──┐
                  └──────────────┘                        │
                         ▲                                ▼
                         │                    ┌────────────────────────┐
                         │                    │ ingest-worker          │
                         │                    │ (Container Apps Job,   │
                         │                    │  KEDA queue-triggered) │
                         │                    └───────────┬────────────┘
                         │                                │
                         │        ┌───────────────────────┼──────────────────┐
                         │        ▼                       ▼                  ▼
                         │  Document Intelligence   Azure OpenAI        Claude via
                         │  (layout, tables,        (embeddings)        Foundry
                         │   bounding boxes)                            (extraction)
                         │        │                       │                  │
                         │        └───────────────────────┼──────────────────┘
                         │                                ▼
                         │                        ┌───────────────┐
                         └────────────────────────│  Cosmos DB    │
                                                  │  (serverless, │
                            ┌─────────────────────│   + vector)   │
                            │                     └───────────────┘
                            ▼                             ▲
                  ┌──────────────────────┐                │
                  │ obligation-scanner   │────────────────┘
                  │ (Container Apps Job, │
                  │  cron: daily)        │
                  └──────────────────────┘
```

**Ingest** — API writes the PDF to Blob, records a `contract` row as `PENDING`,
and enqueues the blob reference. The job pulls it, runs Document Intelligence,
chunks by clause, embeds each chunk, asks the model for structured obligations
grounded in those chunks, validates the citations, and writes everything in one
pass. Status moves `PENDING → EXTRACTING → READY` or `→ FAILED` with a reason.

**Query** — the API serves the obligation register, full-text and vector search
over clauses, and a `/obligations/{id}/evidence` endpoint returning the cited
clause text plus its page and bounding box.

**Monitor** — a daily cron job re-evaluates every open obligation against the
calendar and flags `DUE_SOON`, `OVERDUE`, and `AUTO_RENEWING_SOON`. This is
where the system earns its keep: a contract read once and forgotten is the
default failure mode it exists to fix.

## 3. Azure services, and why each one

| Service | Role | Why this and not the obvious alternative |
|---|---|---|
| **Container Apps** | API + both jobs | Scale-to-zero with a real free grant. Jobs are first-class (queue- and cron-triggered), so the worker isn't a web app pretending to be a worker. |
| **Container Apps Jobs** | ingest, scanner | Event- and schedule-driven, billed per execution. No always-on worker. |
| **Document Intelligence** | layout/table/KV extraction | The core capability. Returns bounding boxes, which is what makes clause-level citation possible at all. |
| **Cosmos DB (serverless, NoSQL)** | contracts, clauses, obligations, audit | **Native vector search (DiskANN)** means one datastore, not a database plus a search service. See §5. |
| **Blob Storage** | raw PDFs | Immutable source of truth; extraction is always re-runnable from the original bytes. |
| **Service Bus** | ingest queue | Decouples upload latency from extraction cost. Dead-letter queue gives failed docs somewhere to be found. |
| **Foundry** | Claude (extraction) + Azure OpenAI (embeddings) | Claude for the reasoning-heavy structured extraction; `text-embedding-3-small` for vectors. Anthropic has no embedding model, so this split is required, not stylistic. |
| **Managed Identity** | all service-to-service auth | No connection strings anywhere. Mirrors the ECS-task-role pattern from `deep-agent-ai`. |
| **Entra ID** | API auth | See §6. |

## 4. Data model

Four Cosmos containers, all partitioned by `/contract_id`:

```
contracts     { id, contract_id, title, counterparty, blob_uri, content_hash,
                status, failure_reason, effective_date, term_end_date, ... }

clauses       { id, contract_id, ordinal, heading, text, page, bounding_box,
                embedding: float[1536] }        ← vector-indexed

obligations   { id, contract_id, description, obligor_party, obligation_type,
                due_date, recurrence, amount, currency, cited_clause_ids[],
                confidence, state, model_version, prompt_version }

audit         { id, contract_id, action, actor, detail, ts }
```

Field names are snake_case because they *are* the Pydantic field names —
documents are stored as `model_dump()` with no mapping layer between the domain
model and the stored shape. A mapping layer is exactly the code that drifts out
of sync with the schema it mirrors. See `docs/DECISIONS.md` 005.

`audit` is the same per-step trail as `riskguard-ai`'s `audit_logs` — every
ingest stage and every state transition writes one row.

**The partition-key tradeoff, stated plainly:** `/contract_id` is right for
ingest and for every per-contract read, which is the overwhelming majority of
traffic. It is *wrong* for the scanner's "all obligations due in the next 30
days" query, which becomes cross-partition. At portfolio scale (hundreds of
contracts) that costs a few RU and is entirely fine. At tens of thousands it
would need a second, date-bucketed index container fed by change feed. Noted
here so the ceiling is a known decision rather than a later surprise.

## 5. RAG without Azure AI Search

The reflexive Azure RAG stack is Azure AI Search. It is skipped deliberately:
its Basic tier bills a flat ~$75/month whether or not a single query runs,
which is the wrong shape entirely for a personal Pay-As-You-Go subscription.

Cosmos DB NoSQL supports vector embedding policies with a DiskANN index and
`VectorDistance()` in queries. One datastore, serverless per-operation billing,
and no synchronization problem between a database and a separate search index.

The tradeoff is real and worth knowing: no BM25 hybrid ranking, no semantic
reranker, no faceting. For clause retrieval over a single contract — a small,
well-scoped corpus — vector similarity plus a metadata filter is sufficient. If
this ever grew into cross-portfolio semantic search over tens of thousands of
documents, AI Search would become the right answer and this is the decision to
revisit first.

## 6. Auth

Same lesson as `riskguard-ai`, applied earlier: the surface is protected from
the first commit rather than retrofitted after it is already internet-reachable.

**Entra ID JWT bearer validation** on the API, with the same inert-until-
configured behavior that worked well in RiskGuard — unset config disables
enforcement so local runs and tests stay frictionless, and startup logs a
warning when that is the case. Service-to-service auth is Managed Identity
throughout; there are no connection strings to leak.

`/healthz` is exempt and liveness-only, because Container Apps' probe cannot
present a credential.

## 7. Build phases

1. **Skeleton + IaC** — `azd up` provisions and deploys a hello-world API.
2. **Ingest path** — upload → Blob → queue → job → Document Intelligence →
   clauses in Cosmos. No LLM yet.
3. **Extraction** — structured obligations via Claude, with citation validation
   rejecting ungrounded output.
4. **Retrieval** — embeddings, vector index, search and evidence endpoints.
5. **Monitoring** — cron scanner, obligation state machine, notifications.
6. **Hardening** — Entra ID auth, audit trail completeness, load/cost check.

Each phase ends deployable, matching the phased approach in `PLAN.md` files
across the workspace.

## 8. Open questions

- **Recurrence modelling.** "Net 30 from each invoice" is not a date; it is a
  rule that needs an anchor event. Phase 5 needs a small recurrence grammar,
  and it is the most likely place for scope to escape.
- **Amendments.** A contract amended by a later document is the normal case,
  not the edge case. Superseding obligations needs a contract-family concept —
  deliberately out of scope until phase 5, but it will not stay out.
- **Confidence calibration.** The extractor returns a confidence score; nothing
  yet establishes what it means. Until it is calibrated against a labeled set,
  it should not gate anything user-visible.
