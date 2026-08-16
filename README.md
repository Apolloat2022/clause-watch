# ClauseWatch

Contract obligation extraction and monitoring, built natively on Azure.

Executed contracts go in. What comes out is a queryable, date-aware register of
**who owes what, when, and under which clause** — where every obligation is
traceable to the page and region of the document it was read from, and a daily
job tells you what is due, overdue, or about to auto-renew.

The problem it exists to fix is not "nobody read the contract." It is that
somebody read it once, eighteen months ago, and nothing has re-read it since.

> **Status: scaffold.** Architecture and project structure are in place; the
> pipeline is not implemented yet. See [Build phases](#build-phases).

---

## Why these Azure services

Full reasoning in [`ARCHITECTURE.md`](ARCHITECTURE.md). The short version:

| Service | Doing what |
|---|---|
| **Container Apps** | FastAPI API, scale-to-zero |
| **Container Apps Jobs** | queue-triggered ingest, cron-triggered obligation scanner |
| **Document Intelligence** | layout, tables, and bounding boxes — the bounding boxes are what make clause-level citation possible |
| **Cosmos DB** (serverless) | contracts, clauses, obligations, audit — *and* vector search, via DiskANN |
| **Blob Storage** | immutable raw PDFs, so extraction is always re-runnable |
| **Service Bus** | ingest queue with a dead-letter path for poison documents |
| **Foundry** | Claude for structured extraction, Azure OpenAI for embeddings |
| **Managed Identity** | every service-to-service call; no connection strings anywhere |

Two cost decisions worth calling out, because they are the difference between
this costing a few dollars a month and roughly eighty:

- **No Azure AI Search.** It is the reflexive Azure RAG answer, and its Basic
  tier bills ~$75/month flat whether or not a query ever runs. Cosmos DB NoSQL
  does vector search natively (DiskANN), so there is one datastore, billed per
  operation, with no index to keep in sync.
- **Cosmos serverless, not provisioned throughput.** Provisioned has a ~400
  RU/s floor billed continuously. Serverless idles at approximately nothing.

`azd down` removes all of it.

## Design commitments

1. **Every obligation cites a clause.** Extraction returns clause ids; an
   obligation citing anything not supplied is rejected at write time, not
   surfaced with a low confidence score. A hallucinated payment term is worse
   than a missed one, because someone may act on it.
2. **Layout is signal.** Payment schedules and SLA tables carry the numbers
   that matter, and flattening them to prose destroys the row/column
   relationships that make them mean anything.
3. **Extraction is replayable.** Documents are content-hashed, so re-ingest is
   a no-op; every obligation records the model and prompt version that produced
   it, so a bad batch is findable and re-runnable.

## Layout

```
app/
  api/         health, contract upload/status, obligation register + evidence
  domain/      models.py — the extraction contract, and the prompt surface
  ingest/      doc_intelligence -> chunker -> embeddings -> extractor
  jobs/        ingest_worker (queue), obligation_scanner (cron)
  data/        cosmos, blob
  auth.py      Entra ID JWT validation, inert until configured
infra/         Bicep — see the validation caveat below
tests/
```

`app/domain/models.py` is the place to start reading. The field descriptions on
`ExtractedObligation` are not documentation — they are handed to the model as
structured-output constraints, so they are prompt surface.

## Local development

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -e ".[api,azure,llm,dev]"
cp .env.example .env                                 # fill in endpoints
uvicorn app.main:app --reload
pytest -q
```

Auth is inert until `ENTRA_TENANT_ID` and `ENTRA_AUDIENCE` are both set, so
local runs need no token. Startup logs a warning when enforcement is off — the
same pattern used in `riskguard-ai`, adopted here from the first commit rather
than retrofitted after the endpoint was already public.

## Deploying

```bash
az bicep build --file infra/main.bicep    # validate FIRST — see caveat
azd auth login
azd up
```

> ⚠️ **The Bicep is unvalidated.** It was authored without access to the target
> subscription. Run `az bicep build` and then `azd provision` before trusting
> it; API versions drift and are the most likely thing to need bumping.

## Auth setup

The ingress is external. Deploy without the two variables below and you publish
an unauthenticated contract-upload endpoint that spends real money on Document
Intelligence and Claude calls — so this is not an optional appendix.

`app/auth.py` installs its middleware only when **both** `ENTRA_TENANT_ID` and
`ENTRA_AUDIENCE` are set, and the Bicep passes them through on the same
condition. Half-configured is off, not partly on. Startup logs a warning when
enforcement is disabled.

**1. Register the API and give it an Application ID URI.**

```bash
APP_ID=$(az ad app create --display-name ClauseWatch --query appId -o tsv)
az ad app update --id "$APP_ID" --identifier-uris "api://$APP_ID"
az ad sp create --id "$APP_ID"
```

`api://$APP_ID` is used rather than a friendlier `api://clausewatch` because the
GUID form is accepted unconditionally; a custom URI has to match a domain
verified on the tenant, which is a separate piece of setup with its own failure
mode.

**2. Issue a secret**, so something can actually ask for a token. For a demo the
app registration is both the API and its own client — no second registration:

```bash
SECRET=$(az ad app credential reset --id "$APP_ID" --query password -o tsv)
```

**3. Hand both values to azd and re-provision.**

```bash
azd env set ENTRA_TENANT_ID "$(az account show --query tenantId -o tsv)"
azd env set ENTRA_AUDIENCE  "api://$APP_ID"
azd provision
```

**4. Get a token and call the API.**

```bash
TENANT=$(az account show --query tenantId -o tsv)
TOKEN=$(curl -s -X POST \
  "https://login.microsoftonline.com/$TENANT/oauth2/v2.0/token" \
  -d "client_id=$APP_ID" \
  -d "client_secret=$SECRET" \
  -d "scope=api://$APP_ID/.default" \
  -d "grant_type=client_credentials" | jq -r .access_token)

curl -H "Authorization: Bearer $TOKEN" "$(azd env get-value SERVICE_API_ENDPOINT_URL)/obligations"
curl "$(azd env get-value SERVICE_API_ENDPOINT_URL)/healthz"   # no token: exempt
```

### When it returns 401

The response body is deliberately identical for every rejection — a specific
error would tell anyone probing the surface exactly what to change next — so the
reason is in the container log, not the response:

```bash
az containerapp logs show --follow \
  --name "$(azd env get-value SERVICE_API_NAME)" \
  --resource-group "$(azd env get-value AZURE_RESOURCE_GROUP)"
```

(`azd monitor` is not the tool here — it opens Application Insights, which this
template deliberately does not provision. Logs go to Log Analytics.)

Failing that, paste the token into [jwt.ms](https://jwt.ms) and check two claims:

- **`aud` must equal `ENTRA_AUDIENCE` exactly.** Depending on the registration
  it is either `api://$APP_ID` or the bare GUID, and they are not
  interchangeable. This is the usual cause.
- **`iss`** will be `https://sts.windows.net/$TENANT/` for a v1 token or
  `https://login.microsoftonline.com/$TENANT/v2.0` for v2, decided by
  `accessTokenAcceptedVersion` in the app manifest. Both are accepted, so there
  is nothing to change here — see [`docs/DECISIONS.md`](docs/DECISIONS.md) 010.

A `503` rather than a `401` means the JWKS endpoint was unreachable. The API
refuses to fail open, so an outage at the identity provider reads as unavailable
rather than as unauthenticated.

> **This is authentication, not authorization.** Any valid token for the
> audience reaches every endpoint; there are no scope or role checks yet.

## Build phases

Each phase ends deployable.

1. **Skeleton + IaC** — `azd up` provisions and deploys a hello-world API.
2. **Ingest path** — upload → Blob → queue → job → Document Intelligence →
   clauses in Cosmos. No LLM yet.
3. **Extraction** — structured obligations via Claude, with citation validation
   rejecting ungrounded output.
4. **Retrieval** — embeddings, vector index, search and evidence endpoints.
5. **Monitoring** — cron scanner, obligation state machine, notifications.
6. **Hardening** — Entra ID auth, audit completeness, load and cost check.

Currently at the start of phase 1.

## Known unknowns

Carried from [`ARCHITECTURE.md`](ARCHITECTURE.md) §8 because they will shape
the design and are better stated than discovered:

- **Recurrence.** "Net 30 from each invoice" is a rule needing an anchor event,
  not a date. Phase 5 needs a small grammar for this, and it is the most likely
  place for scope to escape.
- **Amendments.** A contract amended by a later document is the normal case.
  Superseding obligations needs a contract-family concept — out of scope until
  phase 5, but it will not stay out.
- **Confidence.** The extractor returns a score; nothing yet establishes what
  it means. Until calibrated against a labeled set it should not gate anything
  user-visible.
