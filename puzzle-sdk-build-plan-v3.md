# Puzzle SDK & Gateway — Production Build Plan (v3.1)

**Startup-scoped, production-grade.** This plan ships a working, reliable, observable product fast — without the multi-region, six-service complexity of an infrastructure org that has millions of users. It is "production grade" in the ways that matter at startup scale: it won't fall over, won't lose data, won't need a rewrite to grow. It is not over-built for scale you don't have yet.

**The guiding rule:** build the *boundaries* like a serious system, build the *implementation* like a startup. Correct seams now; heavy machinery later, only when load or customers demand it.

- **[v1]** — ships in the first release.
- **[later]** — the architecture accommodates it cleanly; build when justified by real demand.

---

## Part A — What we are building and why this scope

**The product:** a Python SDK (`puzzle`) plus a backend gateway. A developer writes `puzzle.documents.parse(file="contract.pdf")` and Puzzle routes the request to the best document-intelligence provider (Mistral OCR, Reducto, LlamaParse, Docling, Textract, Mindee, Veryfi, Nanonets), handles fallback if one is down, normalizes the output, and returns one consistent result. One API, one bill, automatic routing.

**The first vertical is `documents`** — OCR, parsing, and typed extraction. Later verticals (`voice`, `agents`) reuse the same core. The vertical is named `documents`, not `ocr`; OCR is just one capability inside it.

**What we deliberately do NOT build in v1:**

- Multi-region / data residency — no customers, no residency obligations yet.
- A fleet of micro-services — three services is enough; six is theater at this stage.
- ML-based routing — heuristics plus eval data are fine until the eval corpus is large.
- More than 8 providers — eval coverage, not provider count, is the moat.
- `voice` / `agents` verticals — stubs only; built after `documents` ships.
- Active-active failover, Kafka, a data warehouse — managed queue + Postgres is plenty.
- **The broad evaluation engine** that scientifically narrows the hundreds of providers in the world down to a recommended set — see Part A.1. v1 builds routing and the eval-data *capture* pipeline; the cross-provider eval *engine* is a later product.

**What we DO build to production grade in v1** (these are non-negotiable because retrofitting them is painful): clean SDK/core/vertical boundaries, durable idempotency, tenant provider sets, durable async jobs, circuit breakers and fallback, a properly isolated credential vault, versioned normalization contracts, telemetry off the critical path, SLOs and monitoring, infra-as-code, a real test suite.

---

## Part A.1 — Two layers: Evaluation vs. Routing (scope boundary)

Puzzle has two distinct layers. They are easy to conflate; they must not be. This plan builds **only Layer 2**.

**Layer 1 — Evaluation: picks the candidate set ("which providers").**
There are hundreds of document-intelligence providers in the world. The evaluation engine uses broad cross-provider benchmarking to decide which handful (~5) are worth a given user's consideration for their use case. This is a *separate Puzzle product*, built later. It owns the full provider catalog, cross-provider discovery, and "should you swap provider X for Y" recommendations.

**Layer 2 — Routing: picks the winner per task ("which one, right now").**
Given the user's chosen set of ~5 providers, for *each individual task* the router wires the request to the best one of those 5 — based on that specific document's features (page count, tables, language, cost cap, latency budget) and the providers' quality scores. **This is what this plan builds.**

```
   hundreds of providers
            │
            ▼
   ┌──────────────────┐   LAYER 1 — Evaluation engine   (separate
   │  eval engine     │   broad benchmarking → recommends ~5   product,
   └────────┬─────────┘                                    built later)
            │  user subscribes to a set of ~5 providers
            ▼
   ┌──────────────────┐   LAYER 2 — Router              (THIS PLAN)
   │  router          │   per task, picks best 1-of-5
   └────────┬─────────┘   using request features + quality scores
            │
            ▼
      one provider serves the task; fallback within the 5 on failure
```

**The subtle point — the router still needs quality data.** Routing is not "the user picked 5, send to whichever is cheapest." For `strategy=highest_quality` or `strategy=balanced` to work, the router must know per-provider, per-document-category quality scores (e.g. "Reducto 0.94 on financial tables vs. LlamaParse 0.81"). So the router *consumes* quality scores — but only for the user's 5, not for all hundreds.

**The user's chosen set is a first-class object.** v1 has a `ProviderSet` per tenant/use case, not an implicit list buried in config. It records enabled providers, BYOK vs. Puzzle-managed credentials, hard denies, data-residency/privacy constraints, capability tags, rollout flags, preferred fallback order, and whether shadow execution is allowed. The router never scores providers outside the active `ProviderSet`.

**One dataset, two scopes.** Layer 1 and Layer 2 read the *same* underlying eval corpus — the corpus fed by shadow execution and ground-truth feedback. The only difference is scope: Layer 1 reads it across all providers to recommend a set; Layer 2 reads it for just the user's 5 to pick a winner. This is why the plan builds the eval-score *store* (Phase 1) and the shadow-execution pipeline that *feeds* it (Phase 2) even though the broad eval *engine* is out of scope: v1's routing produces the eval data as a byproduct, and that data is the foundation the Layer 1 engine is later built on.

**v1 reality check on "which 5":** early on, users will not have a sophisticated candidate-selection problem — they will pick the obvious providers (Mistral for cost, Reducto for accuracy, etc.) or accept a Puzzle-curated default set. The scientific narrowing of hundreds → 5 only becomes valuable once there genuinely are hundreds of providers worth choosing among *and* the eval corpus is large enough to make the recommendation credible. That is precisely why Layer 1 is deferred and Layer 2 ships first.

| | Layer 1 — Evaluation engine | Layer 2 — Router (this plan) |
|---|---|---|
| Question answered | Which ~5 providers should this user use? | Which 1 of the 5 serves this task? |
| Scope of eval data read | All hundreds of providers | Only the user's 5 |
| Granularity | Per user / use case | Per individual task |
| In v1? | No — separate later product | Yes |
| Relationship | Consumes the eval corpus broadly | Consumes the eval corpus narrowly; also *produces* it via shadow execution |

Nothing elsewhere in this plan changes. Phase 1 builds the eval-score store, Phase 2's routing engine reads it, and shadow execution keeps it fed. This section exists only to make the boundary unambiguous so the team never wonders "does the router need eval or not?" — it does, but only for the 5, and the data is already being captured.

---

## Part B — Architecture

### B.1 The shape: three services, one region

```
                  ┌─────────────────────┐
                  │  Load Balancer      │
                  │  + TLS + basic WAF  │
                  └──────────┬──────────┘
                             │
   ┌─────────────────────────────────────────────────────┐
   │  SERVICE 1 — API + Routing  (stateless, autoscaled)  │
   │  ───────────────────────────────────────────────    │
   │  • auth, request validation                         │
   │  • rate limiting, per-tenant quota, idempotency      │
   │  • spend circuit breaker                             │
   │  • routing engine: feature extraction, provider      │
   │    scoring, fallback orchestration                   │
   │  • provider adapters: outbound calls + normalization │
   │  • per-provider circuit breakers                     │
   ├─────────────────────────────────────────────────────┤
   │  SERVICE 2 — Job Orchestrator (workers + queue)      │
   │  • long-running async parse jobs                     │
   │  • durable job state, crash recovery                 │
   ├─────────────────────────────────────────────────────┤
   │  SERVICE 3 — Telemetry/Eval Ingest (queue consumer)  │
   │  • async ingestion of request events, shadow         │
   │    results, ground-truth feedback                    │
   └─────────────────────────────────────────────────────┘

   Shared datastores (managed cloud services):
   • Postgres        — tenants, ProviderSets, durable idempotency records,
                       jobs, billing ledger, eval scores
   • Redis           — rate limits, circuit-breaker state,
                       hot idempotency/result cache
   • Managed queue   — job tasks + telemetry events
                       (SQS / Pub-Sub — not Kafka)
   • Object store    — document bytes, large artifacts

   Separate subsystem:
   • Docling model server — GPU pool for self-hosted OCR
```

**Why three services and not one, and not six:**

- **API + Routing is one service.** At startup scale, splitting "API gateway" from "routing" from "adapters" buys you nothing but three deployment pipelines. They scale together, they fail together, keep them together. *Inside* the codebase they are clean modules with clean interfaces — so splitting later is a refactor, not a rewrite.
- **Job Orchestrator is separate** because async parse jobs run for minutes and must survive a deploy or crash. Coupling them to the stateless request service means a deploy kills in-flight jobs. This separation is worth it on day one.
- **Telemetry ingest is separate** because it consumes a queue at its own pace. The request path publishes an event and moves on — it never waits on telemetry. This keeps request latency independent of analytics load.

That is the right number of seams for a startup: enough that the painful-to-retrofit boundaries exist, few enough that you are not running an ops zoo.

### B.2 The SDK ↔ Gateway split

- **`puzzle`** — the Python SDK, open source, on PyPI. A thin, typed client over the gateway's HTTP API.
- **`gateway`** — the three backend services. Private.
- Routing logic lives server-side so it improves without users upgrading the SDK.
- `shared/schemas/` holds the Pydantic models both sides import — the contract never drifts.

### B.2.1 Adapter/execution ownership

Provider adapters live in a shared gateway library, not inside only one service. The API + Routing service imports the library for synchronous requests; Job Orchestrator workers import the same library for async jobs. The routing engine produces a frozen `RoutingDecision` with ordered attempts, constraints, and cost ceilings; the executor consumes that decision and records each attempt. This avoids a hidden service-to-service call from workers back into the public API while keeping adapter behavior identical across sync and async paths.

### B.3 Self-hosted Docling

Docling (open-source OCR) needs a GPU and is therefore its own small subsystem, not just an adapter:

- A small GPU node pool, scale-to-floor (never zero — cold starts are minutes), a queue in front to absorb bursts.
- Model loaded at node startup; health check gates traffic until loaded; per-request timeout and an OOM guard so one runaway document can't kill the node.
- The Routing service treats it as just another provider with a health signal and a capacity ceiling.
- **[v1]** single GPU pool. It is the *last* routing choice unless explicitly requested or required for an on-prem/privacy reason — GPU time is your most expensive option.
- **Launch-risk rule:** Docling is important, but the production GPU pool must not block the rest of v1. If GPU autoscaling/OOM hardening slips, ship Docling behind an explicit beta flag or managed allowlist while the hosted providers remain generally available.

### B.4 Region-agnostic, so multi-region stays cheap later

No component hard-codes a region or assumes anything beyond "one region exists." Tenant records have a `region` column (set to the single region for now). When a customer eventually needs EU residency, adding a region is: stand up the same stack via Terraform elsewhere, route by tenant `region`. **[later]** — designed-for, not built.

---

## Part C — The request lifecycle

A synchronous `parse` call, end to end:

1. **Load balancer** terminates TLS, applies basic WAF rules.
2. **API + Routing service**: authenticate (API key → tenant); validate the request; check **rate limit** and **quota** (Redis token bucket); check the **idempotency key** in Postgres as the durable source of truth, with Redis as a hot cache. If seen before, return the stored result instead of re-processing and re-charging.
3. Check the **spend circuit breaker**: if the tenant or platform has blown a spend threshold this window, reject or force cheap-only routing.
4. Document bytes are written to the **object store**; only a reference travels onward.
5. **Routing engine**: extract request features (page count, file type, language, layout complexity); load the tenant/use-case `ProviderSet`; read provider health (Redis) and eval scores (Postgres); filter on hard constraints; score candidates; produce an ordered, frozen `RoutingDecision`.
6. **Provider executor** calls the chosen provider through the shared adapter library, wrapped in a **circuit breaker** (open after repeated failures → skip and fail over instantly) and a timeout.
7. On failure, walk the fallback list; record every attempt.
8. Normalize the response → `PuzzleDocument`; optionally cache in Redis (content-hash key).
9. Publish a **telemetry event** to the queue — fire-and-forget. The request path does not wait.
10. Return the result with `request_id`, `route_decision`, `attempted_providers`, `usage`.

Async `parse.submit` diverges after step 5: the frozen routing decision and object reference are handed to the **Job Orchestrator**, which writes job state to Postgres, enqueues the work, and returns a job handle immediately. Workers execute the same shared provider adapter library used by the sync path; job state is durable; a worker crash causes redelivery, not loss.

---

## Part D — The production-grade essentials (startup-sized)

These are the things that make it "production grade" without making it "FAANG infra." Each is scoped to startup reality.

### D.1 Reliability

- **Idempotency** — every billable operation takes an idempotency key; Postgres is the durable source of truth with unique constraints, stored response/result references, and billing linkage. Redis may accelerate lookups, but never owns correctness. Never double-charge, never double-process.
- **Circuit breakers** — per provider; an unhealthy provider is removed from routing instantly and probed for recovery.
- **Fallback chains** — every routing decision is an ordered list; failure walks it; all attempts recorded.
- **Durable async jobs** — job state in Postgres, work in the queue; survives deploys and crashes.
- **Graceful degradation** — if eval scores are unavailable, routing falls back to static vendor-benchmark priors; if Redis is down, durable idempotency still works via Postgres while rate-limit/circuit-breaker checks fail closed or force a conservative cheap-only route for billable ops.
- **SLOs** — three, tracked from launch:
  - API availability: **99.5%** (startup-honest, not 99.99%).
  - Routing overhead P95 (excluding the provider call itself): **< 50ms**.
  - Successful parse rate (excluding provider-side errors): **99%**.
  - When an SLO's error budget is spent, reliability work takes priority over features.

### D.2 Security & data

- **BYOK vault** — credentials envelope-encrypted (data key per credential, master key in cloud KMS). Never logged, never in responses or URLs. The SDK passes a vault reference ID after first registration. **[v1]** the vault is a hardened *module* with least-privilege access, not yet a separate service — splitting it out is **[later]** if a security review demands it.
- **Document lifecycle** — bytes stored in the object store; default retention = purge after processing + a short grace window; tenant-configurable.
- **Deletion** — a deletion request erases document bytes, job payloads, cached results, and redacts derived eval data; emits a completion record. Real pipeline, not a sentence.
- **Tenant isolation** — every datastore query is tenant-scoped; isolation is tested in CI.
- **Transport** — TLS everywhere; no document content in URL parameters.
- **Audit log** — append-only record of requests, routing decisions, vault access, config changes.
- **Provider ToS** — before launch, confirm each provider permits proxying; BYOK mode is unaffected regardless.
- **Shadow execution consent** — shadow execution is tenant-configured and policy-bound. The policy records sampling rate, allowed providers, cost cap, PII/data restrictions, and whether raw bytes may be sent to a second provider.
- **SOC 2** — **[later]**, but logging and access control are designed now so the eventual audit is cheap.

### D.3 Cost governance (this is your COGS)

- **Per-tenant spend quota** enforced in the request path.
- **Platform spend circuit breaker** — if aggregate provider spend spikes (e.g. a bug loops requests to the priciest provider), it trips and forces cheap-only mode or rejects.
- **Cost normalization** — every provider's billing unit (per-page, per-document, per-credit, per-token) converts to a `cost_per_page_equivalent`, unit-tested, so routing and billing reason in one currency.
- **Margin visibility** — the billing ledger records both tenant charge and provider cost per request.

### D.3.1 Normalization contract

- **Schema versioning** — every normalized result includes `normalized_schema_version`, provider name, provider API/model version, adapter version, and normalization warnings.
- **Raw fidelity** — `result.raw` preserves the complete provider response, subject to tenant retention settings and deletion policy.
- **Adapter snapshot tests** — each adapter has golden input/output fixtures so provider API drift breaks CI before it breaks customers.

### D.4 Observability

- **Structured JSON logs** — one event per request: `request_id`, tenant, provider, latencies, cost, route decision.
- **Tracing** — OpenTelemetry spans SDK → gateway → adapter → provider.
- **Metrics** — request rate, error rate, latency percentiles per service; per-provider success rate, latency, fallback rate, circuit-breaker state.
- **Dashboards + alerting** — SLO burn, latency regression, error spikes, vault errors, spend anomalies. **[v1]** a managed observability stack (Grafana Cloud / Datadog) — do not self-host monitoring at this stage.
- **Provider health board** — live provider status; feeds the routing availability signal.
- **On-call** — a lightweight rotation and runbooks. At a 2–4 person team this is "whoever is awake plus a runbook," made formal as the team grows.

### D.5 Capacity (startup-honest targets)

- v1 target: **25 RPS sustained, 100 RPS peak**. That covers a real early customer base; do not design for more yet.
- Routing overhead P95 < 50ms; end-to-end latency dominated by the provider (1–8s).
- Every stateless service starts at 2 instances (no single point of failure) and autoscales on CPU.
- Postgres: a managed instance plus one read replica; partition the `jobs` and `telemetry` tables by month so they stay manageable.
- This is enough. Load testing in Phase 4 validates it. **[later]** revisit when real traffic shows the shape.

---

## Part E — The phased roadmap (do these in order)

Seven phases. Each has a single clear goal and a hard **Definition of Done (DoD)**. **Do not start a phase until the previous DoD is met.** This ordering is the actual answer to "what do we do, step by step, to ship a working product."

### Phase 0 — Foundations (Week 1)

**Goal:** make all later work fast and safe.

- Monorepo; `uv` for environments; `ruff` + `mypy --strict` + `pytest`; CI that runs lint → typecheck → tests → build on every PR.
- Terraform for the single region: network, Postgres, Redis, managed queue, object store, KMS. Infrastructure is code from day one.
- A staging environment and a deploy pipeline.
- `shared/schemas/` skeleton; canonical error hierarchy.
- Trial accounts for all 8 providers; full smoke verification required for the first 4 parse providers, and account/API/ToS/pricing verification required for the 4 extraction providers that land in Phase 3.

**DoD:** `git push` → green CI → deployable image in staging. Terraform stands up the whole region reproducibly. The first 4 parse provider keys are smoke-tested end to end; the remaining 4 extraction providers have accounts, API access, pricing, and ToS checked before Phase 3.

### Phase 1 — The routing core (Weeks 2–4)

**Goal:** a vertical-agnostic platform that can route, fall back, retry, and record — with **no document logic yet**.

- SDK `_core`: `Client` / `AsyncClient` over httpx (identical sync/async surfaces); retries with backoff + `Retry-After`; canonical error mapping; request IDs; OTEL spans.
- API + Routing service: auth, validation, **rate limiting, quota, idempotency**, spend circuit breaker; the routing-engine *interface* and scoring framework; fallback executor; per-provider circuit breakers.
- `ProviderSet` model and admin/config path: enabled providers, credential mode, hard denies, regional/privacy constraints, fallback preferences, rollout flags, and shadow policy.
- Job Orchestrator: durable job state machine, queue-driven workers, crash recovery.
- Telemetry ingest: queue consumer writing to Postgres.
- BYOK vault module: envelope encryption, scoped access, rotation.
- Cost-normalization framework.

**DoD:** a *mock* provider routes end-to-end through the gateway. Fallback verified by fault injection (kill the mock primary → the secondary serves). Idempotency verified against Postgres uniqueness and stored result references (a replayed request returns the stored result, no re-charge, even if Redis is empty). A job survives an orchestrator-worker kill and still completes. Workers execute the same shared adapter/executor contract as the sync path. Telemetry flows through the queue, never on the request path. The vault stores and retrieves an encrypted credential under least privilege. **The core compiles and its tests pass with zero imports from `documents/`.** >85% test coverage.

> This DoD is the single most important gate in the plan. If the core depends on document logic, the multi-vertical thesis is already dead. Enforce it strictly.

### Phase 2 — The documents vertical, parsing path (Weeks 5–8)

**Goal:** a real product — parse documents through real providers.

- `shared/schemas/documents.py`: `PuzzleDocument`, `Element`, `BoundingBox`, `Chunk`, `TypedField`, `ParseResult`, `Job`, `normalized_schema_version`, provider metadata, adapter metadata, and normalization warnings.
- SDK `documents`: `parse` (sync convenience + `submit` for async); `jobs`; named, versioned routing `strategies` (`balanced`, `cheapest`, `highest_quality`, `regulated`); `explain_routing()`; `report()` for ground-truth feedback.
- First four provider adapters, in this order: **Mistral OCR 3 / current OCR alias** (target the documented alias such as `mistral-ocr-latest`, and record the resolved model version; cleanest API, the cost default), **Docling** (self-hosted; doubles as the eval reference), **Reducto** (premium accuracy, bounding boxes), **LlamaParse** (RAG markdown, user demand).
- The Docling GPU model-serving subsystem (Part B.3), with beta/allowlist fallback if production hardening is not ready.
- Concrete routing scoring; request-feature extraction; sync file-size/page-count/time limits; automatic async handoff for large jobs; client-side PDF splitting for files over a provider's limit.
- Content-hash result cache.

**DoD:** `puzzle.documents.parse(file="contract.pdf")` works with zero config, via explicit `provider=`, and via `strategy=` auto-routing — for all generally available parse providers. Fallback chains work. `result.normalized` is verified correct against a golden corpus of ~30 hand-checked documents; `result.raw` preserves full provider fidelity; adapter snapshot tests cover normalized and raw outputs. Shadow execution runs only when the tenant `ProviderSet` permits it and populates `cross_provider_agreement` without violating cost or data policies. Docling is either production-ready with autoscaling/OOM survival or clearly marked beta/allowlist. >85% coverage on the vertical.

### Phase 3 — Typed extraction (Weeks 9–11)

**Goal:** support the Mindee / Veryfi / Nanonets style of typed field extraction, plus the enterprise BYOK path.

- `client.documents.extract(file, schema)` → a validated Pydantic instance, plus `normalized.fields` with per-field bounding-box provenance.
- Adapters: **Mindee**, **Veryfi**, **Nanonets**, **AWS Textract**.
- Extraction-aware routing — a `schema` argument routes to vertical extractors; `output_format="markdown"` routes to parsers. The router reads intent from the call shape.
- Cost normalization across page-priced and document-priced providers (Veryfi prices per document, not per page), unit-tested.

**DoD:** `extract` returns validated typed objects from all three extractors with bbox provenance. The Textract BYOK path works with a customer's own AWS credentials. Cost normalization is correct across every billing unit.

### Phase 4 — Hardening (Weeks 11–12, overlaps Phase 3)

**Goal:** prove the system holds up before real users touch it.

- **Load testing** to the capacity targets (Part D.5); confirm routing overhead P95 < 50ms.
- **Fault-injection / chaos testing**: kill services, fail Redis, induce provider outages — verify SLOs hold or degrade gracefully.
- **DR check**: restore Postgres and the vault from backup into a clean environment; verify integrity. Backups: continuous WAL archiving + daily snapshot; target RPO 5 min, RTO 1 hour.
- **Deletion pipeline** verified end to end (request → erasure → completion record).
- SLO dashboards and alerting live; runbooks written; the on-call rotation defined.

**DoD:** load and chaos targets met. DR restore succeeds. Deletion pipeline verified. Dashboards and alerts live.

### Phase 5 — Launch surface & docs (Weeks 12–13)

**Goal:** make the product usable and discoverable.

- **MCP server** (`puzzle-mcp`) — so any MCP-compatible agent can call `documents.parse` / `extract` as tools out of the box. ~1 week, unlocks a whole distribution channel.
- **A2A Agent Card** — publish Puzzle's card at `/.well-known/agent.json`. Signals positioning for the agent-economy future. ~1 day.
- **Methodology surface** — `client.documents.methodology.get(...)` plus a public benchmark page in `benchmarks/`. Transparency is the credibility foundation for the eventual trust-layer business.
- **Docs** — a quickstart, three cookbook recipes ("RAG markdown", "typed invoice fields", "on-prem with Docling"), full API reference, a provider capability matrix, a status page.

**DoD:** `pip install puzzle` works; the MCP server is published; the A2A card is live; docs cover the three core use cases; the benchmark page is published.

### Phase 6 — Soft launch & the eval flywheel (Week 14)

**Goal:** get real workloads in and start the data moat.

- Public PyPI release of `puzzle` v1.0.0.
- Onboard 8–12 design partners — get them onto BYOK, running real documents.
- Design-partner requests run **sampled, opt-in shadow execution** against a second provider according to the tenant's `shadow_policy` — this seeds the eval corpus that becomes the routing moat without surprising customers or doubling spend by default.
- Set up a tight feedback loop: which provider-native fields do partners reach into `result.raw` for (those become normalization candidates), and which fallback chains do they configure (those reveal real failure modes).

**DoD:** v1.0.0 on PyPI. Design partners running real workloads. The eval corpus is accumulating from consented shadow execution and ground-truth feedback. A weekly review of partner feedback is running.

---

## Part F — Timeline summary

| Phase | Weeks | Outcome |
|-------|-------|---------|
| 0 — Foundations | 1 | Monorepo, CI/CD, Terraform region, schemas, provider keys |
| 1 — Routing core | 2–4 | Three services, SDK core, durable idempotency, ProviderSets, circuit breakers, durable jobs — vertical-agnostic |
| 2 — Documents (parsing) | 5–8 | `parse` + first parse providers + Docling GA/beta path + routing engine |
| 3 — Typed extraction | 9–11 | `extract` + Mindee/Veryfi/Nanonets/Textract |
| 4 — Hardening | 11–12 | Load + chaos + DR + deletion verified (overlaps Phase 3) |
| 5 — Launch surface | 12–13 | MCP server, A2A card, methodology page, docs |
| 6 — Soft launch | 14 | PyPI release, design partners, eval flywheel running |

**~14 weeks with 3 engineers.** With 2, expect ~20 weeks and defer the Docling GPU autoscaling refinement and one or two extraction providers into a Phase 7. Solo: this is a 6-month plan and you should cut to 4 providers total for v1.

---

## Part G — Definition of "production ready" for the startup-scale v1

- [ ] `pip install puzzle`; `client.documents.parse(file=...)` works with zero config.
- [ ] Three services, cleanly separated; the API+Routing codebase has clean internal module seams.
- [ ] 8 providers integrated; both `parse` (parsing) and `extract` (typed) work.
- [ ] Strategy-based auto-routing with explainable decisions; fallback verified by fault injection.
- [ ] Sync and async/job interfaces; durable jobs survive a deploy or crash.
- [ ] Durable Postgres-backed idempotency on every billable operation, with Redis only as an acceleration layer.
- [ ] Tenant/use-case `ProviderSet` controls provider eligibility, credentials, constraints, fallback preferences, rollout flags, and shadow policy.
- [ ] Rate limiting + per-tenant quota; per-tenant + platform spend circuit breakers.
- [ ] BYOK vault: envelope-encrypted, least-privilege, rotation, compromise runbook.
- [ ] Normalized `PuzzleDocument` is versioned and verified against a golden corpus; `raw` preserves fidelity; adapter snapshot tests catch provider drift.
- [ ] Telemetry off the request critical path; eval corpus accumulating via consented/sampled shadow execution.
- [ ] Three SLOs instrumented with error-budget tracking; dashboards + alerting live.
- [ ] Backups + a passed DR restore; a real deletion pipeline.
- [ ] Infra as code; the region is reproducible from scratch.
- [ ] Load + chaos tested to startup-scale targets.
- [ ] The core compiles and tests with zero imports from `documents/`.
- [ ] MCP server published; A2A Agent Card live; docs cover the three core use cases.
- [ ] Provider ToS reviewed; 8–12 design partners running real workloads.

---

## Part H — What is deliberately deferred, and why that is correct

| Deferred | Why it is fine to defer | When to build it |
|----------|------------------------|------------------|
| Multi-region / data residency | No customers, no residency obligations; code is region-agnostic so it stays a deployment exercise | First enterprise/EU customer who requires it |
| Splitting API and Routing into separate services | Clean internal modules make this a refactor, not a rewrite | When their scaling profiles genuinely diverge |
| ML-based routing | Heuristics + eval data work fine until the corpus is large | When the eval corpus is big enough to train on |
| Layer 1 evaluation engine (broad cross-provider narrowing of hundreds → ~5) | v1's routing produces the eval corpus as a byproduct; the broad engine is only credible once that corpus is large and there are genuinely hundreds of providers worth choosing among (see Part A.1) | After v1 has accumulated a substantial eval corpus from real workloads |
| `voice` / `agents` verticals | The core is built vertical-agnostic, so adding one is additive | After `documents` v1 is in production |
| SOC 2 Type II | Logging and access control are designed now so the audit is cheap later | When enterprise sales requires it |
| Kafka / data warehouse | A managed queue + partitioned Postgres handles startup volume | When telemetry volume outgrows Postgres |
| Active-active failover | A single region with good backups + DR is acceptable at this stage | When an SLA commitment requires it |
| 9th+ provider | Eval coverage, not provider count, is the moat | When real routing data shows a coverage gap |

**The principle holds throughout:** build the boundaries like a serious system so nothing here requires a rewrite — but build the implementation like a startup, shipping a working product in 14 weeks rather than an over-engineered platform in 9 months. Every deferral above is a named decision with a clear trigger, not an omission.
