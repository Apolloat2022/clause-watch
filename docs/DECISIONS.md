# Decision log

One entry per decision that would otherwise get re-litigated in three months.
Newest last.

---

## 001 — Cosmos DB vector search instead of Azure AI Search

**Date:** 2026-08-15 · **Status:** accepted

Azure AI Search is the reflexive answer for RAG on Azure. Rejected on cost
shape: its Basic tier bills roughly $75/month flat regardless of query volume,
which is wrong for a personal Pay-As-You-Go subscription that idles most of the
time. Cosmos DB NoSQL supports vector embedding policies with a DiskANN index
and `VectorDistance()` in queries, giving one datastore, per-operation billing,
and no database-to-index synchronization problem.

**Given up:** BM25 hybrid ranking, semantic reranking, faceting. Acceptable
because clause retrieval is scoped to a single contract — a small, well-bounded
corpus where vector similarity plus a metadata filter is enough.

**Revisit when:** search goes cross-portfolio over tens of thousands of
documents. That is the point where AI Search becomes correct.

---

## 002 — Partition everything by `/contractId`

**Date:** 2026-08-15 · **Status:** accepted

Right for ingest and every per-contract read, which is nearly all traffic.
Knowingly wrong for the scanner's "all obligations due in the next 30 days",
which becomes a cross-partition query.

**Given up:** efficient cross-contract date queries. At hundreds of contracts
this costs a few RU and is fine.

**Revisit when:** contract count reaches five figures. The fix is a
date-bucketed index container fed by change feed, not a re-partition.

---

## 003 — Claude for extraction, Azure OpenAI for embeddings

**Date:** 2026-08-15 · **Status:** accepted

Not a preference — Anthropic serves no embedding model, so the split is forced.
Both live in the same Foundry resource. Claude takes the reasoning-heavy
structured extraction, where the existing prompt patterns from `riskguard-ai`
carry over largely intact; `text-embedding-3-small` produces the vectors.

**Given up:** single-provider simplicity. Two SDKs in `ingest/`.

---

## 004 — Reject ungrounded obligations rather than scoring them down

**Date:** 2026-08-15 · **Status:** accepted

An obligation citing a clause that was not supplied raises `CitationError` and
is discarded, rather than being stored with low confidence and a caveat in the
UI. A hallucinated payment term is worse than a missing one: a missed
obligation is a known unknown, while an invented one is a confident lie someone
may act on.

**Given up:** recall. Obligations assembled across clauses the retriever failed
to surface are lost silently.

**Revisit when:** there is a labeled set to measure the recall cost against.
Until then this errs deliberately toward precision.

---

## 005 — Partition on `/contract_id`, store `model_dump()` unmapped

**Date:** 2026-08-15 · **Status:** accepted · **Supersedes part of 002**

The architecture originally specified `/contractId`, following the camelCase
convention common in Cosmos samples. Changed to `/contract_id` when the
adapters were written, so the partition key path is the Pydantic field name and
documents can be persisted as `model_dump(mode="json")` directly.

The alternative was a `to_document` / `from_document` mapping layer at the
repository boundary. Rejected: a hand-maintained field map between a domain
model and its stored shape is precisely the code that drifts — a field added to
the model and forgotten in the map fails silently at write time, and the loss is
invisible until something reads it back.

**Given up:** conformance with the camelCase convention in most Cosmos
documentation. Cosmos itself is indifferent to naming style.

**Note:** this changes the partition key path, which is fixed at container
creation. Containers already provisioned under `/contractId` must be recreated,
not altered. Nothing is deployed yet, so the cost today is zero.

---

## 006 — Queue leases instead of `receive() -> list[str]`

**Date:** 2026-08-15 · **Status:** accepted

The original `IngestQueue` port returned contract ids. That cannot be
implemented correctly over Service Bus: returning the id means settling on
receipt, so a crash mid-ingest drops the contract with no trace. The in-memory
deque hid the problem completely — it has no concept of settlement, so the port
looked fine until a real broker was put behind it.

Replaced with `LeasedMessage`, which the caller must `complete()` or
`abandon()`. The worker abandons on failure so the broker's delivery count
advances and a poison document reaches the dead-letter queue.

**Given up:** a simpler port, and the ability to ignore settlement in the local
adapter (which now honors `abandon()` by re-queueing).

**Worth noting as a pattern:** the local adapter was not just a test double, it
was actively concealing a design flaw. A port shaped around the weaker
implementation will not survive the stronger one.

---

## 007 — Abandon on failure rather than dead-letter immediately

**Date:** 2026-08-15 · **Status:** accepted

`run_ingest` failures abandon the message rather than dead-lettering it
directly. Distinguishing a transient Document Intelligence 503 from a scanned
PDF that will never parse needs error classification the pipeline does not have
yet, so `maxDeliveryCount` (5, in Bicep) makes the call instead.

**Given up:** a permanently-poison document is retried four more times before
dead-lettering. Cheap, and not hidden: the contract row is marked FAILED with a
reason after the first attempt, so the failure is visible immediately regardless
of what the queue does afterward.

**Revisit when:** retry cost becomes measurable, or Document Intelligence error
codes are being handled specifically.

---

## 008 — No placeholder image on the jobs, and a pinned image tag

**Date:** 2026-08-16 · **Status:** accepted

`azd` sets `SERVICE_API_IMAGE_NAME` during `azd deploy`, not `azd provision`, so
the very first provision runs with no image in the registry. The standard azd
template answer is a public quickstart image as the parameter default.

That is right for the API and wrong for the jobs, and the asymmetry is the whole
decision. A Container App whose image cannot be pulled fails the deployment
outright, so the API needs *something* pullable to bootstrap — and `azd deploy`
replaces it minutes later in the same `azd up`, so it never survives a run. The
jobs are not azd services and nothing ever replaces their image: `azd` only
updates the resource tagged `azd-service-name`. A job left on the quickstart
image stays there permanently. It starts on schedule, exits zero, and drains
nothing — a worker that reports success while doing nothing is worse than one
that crashes, because nothing surfaces it.

So the jobs reference the real registry repository instead, and the worst case
becomes a loud `ImagePullBackOff` on the first execution. Naming that reference
in Bicep before the image exists requires knowing the tag, and azd's default is
`azd-deploy-<unix time>`, which nothing can predict — hence the pin of
`docker.image` and `docker.tag` in `azure.yaml`.

**Given up:** two things. `:latest` means a job execution is not pinned to an
immutable digest, so a re-push moves what the next execution runs — there is no
rollback by tag. And `azure.yaml`'s pin and `builtImage` in
`infra/modules/resources.bicep` must now change together. Both ends carry a
comment saying so, which is a weaker guarantee than not having the coupling.

**Revisit when:** rollback matters, or job executions need to be reproducible
against a known image. The alternatives are threading
`SERVICE_API_IMAGE_NAME` as the sole source and accepting that first-provision
jobs point at a tag that does not resolve until the second provision, or making
both jobs azd services and paying for three pushes of identical bytes.

---

## 009 — The developer principal gets the app's data-plane roles

**Date:** 2026-08-16 · **Status:** accepted

Cosmos, Storage and Service Bus are all provisioned with local auth disabled —
`disableLocalAuth`, `allowSharedKeyAccess: false`. That is the right posture and
it has a consequence worth writing down, because it looks like a mistake to
anyone reading the template later: **there is no key to fall back on.** Owner on
the subscription is a control-plane role and grants nothing on the data plane, so
without an explicit assignment the person who deployed the system cannot read a
document out of Cosmos or a PDF out of Blob. Not "cannot do it conveniently" —
cannot do it at all.

So `principalId` / `principalType` (mapped from `AZURE_PRINCIPAL_ID` and
`AZURE_PRINCIPAL_TYPE`, which azd sets) get the same four data-plane roles as the
app identity, guarded by `if (!empty(principalId))` so a pipeline that does not
set them provisions cleanly without. `principalType` matters and is easy to get
wrong: a service principal assigned as `User` fails on replication delay, which
surfaces as an unrelated-looking error in CI.

**Given up:** least privilege. The developer principal holds Cosmos Data
Contributor, Blob Data Contributor, Service Bus Data Owner and Cognitive
Services User — write access throughout, where read-only variants exist and would
be tighter. Justified for now because testing the ingest path means writing:
enqueueing a message and putting a PDF in Blob by hand is how the pipeline gets
exercised before there is a UI to do it.

**Revisit when:** more than one person has access, or this stops being a personal
subscription. At that point it becomes a group assignment rather than a
per-principal one, and separating the read case from the write case starts to be
worth the second set of roles.

---

## 010 — Bearer validation fails closed, and pins the algorithm

**Date:** 2026-08-16 · **Status:** accepted

Pure-ASGI rather than `BaseHTTPMiddleware`, so it sits in front of routing and
covers every path — including ones no router claims — and never wraps a
streaming response. Five choices inside it are worth stating, because each one
looks like an over-complication until it doesn't:

**RS256 is pinned, and the token header does not get a vote.** A token declaring
`alg: HS256` would otherwise be verified with the RSA *public* key as an HMAC
secret. That key is published at the JWKS endpoint, so forgery reduces to
arithmetic. `tests/test_auth.py` builds that exact token by hand — PyJWT refuses
to sign it, and an attacker has no reason to use PyJWT.

**Both issuer forms are accepted.** A v2.0 token carries the
`login.microsoftonline.com` issuer; a v1 token carries `sts.windows.net`. Which
one arrives is decided by `accessTokenAcceptedVersion` in the app registration
manifest — a different system entirely — so pinning one form produces an auth
failure whose cause is invisible from here.

**A JWKS outage is a 503, never a pass.** Failing open would mean an incident at
the identity provider silently unauthenticates the API, which is the failure
mode where nobody finds out.

**An unrecognised `kid` can force one refetch, no more than once every five
minutes.** Keys rotate, so refetching has to be possible. But an unrecognised
`kid` is also precisely what a forged token looks like, and without the floor an
unauthenticated caller can drive unbounded outbound requests — a
denial-of-service against the login endpoint with this service as the amplifier.

**The 401 body says nothing.** "Expired", "wrong audience" and "unknown key" all
render identically. A specific error tells whoever is probing the surface exactly
what to change next, which is a free oracle. The reason goes to the log.

**Given up:** this is authentication, not authorization. Any valid token for this
audience reaches every endpoint; there are no scope or role checks. There is
also no revocation — a stolen token is good until `exp`.

**Revisit when:** there is more than one class of caller. That is the point where
scope claims start meaning something and the middleware needs a per-route
policy rather than a single gate.
