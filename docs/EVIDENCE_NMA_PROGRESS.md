# Rinvoq/Skyrizi Evidence + NMA programme — build progress

Tracks delivery against the plan (~32 dw base, ~38–42 dw contingency-loaded pilot).
Critical path: `0 → 2 → 3A → (3B ∥ 4) → 6 → 7 → 8 → 9`.

Verify with `python -m pytest -q` from `backend/` (venv is at the **repo root**: `..\.venv`).
Currently **1349 passed, 7 skipped**. The 7 skips are the netmeta parity gate, which needs a
live sidecar (`NMA_SIDECAR_URL`) and is skipped rather than faked — see *Closing the gaps*.

**`origin/main` is `db939af`** — *"Make the corpus growable and the governance gates openable
from the UI"*, 18 files, +3727/-97. Everything in this file is pushed. A push to `main` runs the
pipeline's two test steps and then rsyncs + rebuilds the EC2 container, so code deploys
itself; data does not travel with it, because the rsync excludes `data/` and `*.db` and dev
and production are **different databases**.

**The `fdecb74` push was the first to build the NMA sidecar on the box** — a full R stack
install from pinned CRAN versions, several minutes, ~8-10 GB peak against the ~44 GB free
after the EBS resize. Later deploys skip it via the content hash. If it fails the
application still deploys and the script prints `DEGRADED` at the end rather than aborting,
so **check the last lines of the deploy log, not just that it went green**.

**Production data, after the first prod run.** The re-parse is committed there and
`NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16` is built: 37 studies, **0 orphan rows** (was 664), 296
canonical rows, 131 arms of which 74 still carry no denominator. 10 nodes, connected, **0
independent loops**, so no inconsistency assessment is possible. It is **not** a star —
`is_simple_star` is false, because the network carries a multi-arm trial (`loop_count` 1,
`has_multi_arm_studies` true). Those are two different facts and conflating them is a live
trap: zero *independent* loops means inconsistency cannot be **tested**, while a multi-arm
trial means netmeta is **required**. Under
`PSA_ACR50_W16_PRIMARY` (weeks 14-18) it drops to 8, losing **ABT-122 and Rinvoq** (issue 1,
live in prod and worse than dev recorded). **Nothing is verified**, so evidence gathering
skips every study and no comparison resolves at all — see *Study curation* below for why that
is the verification gate rather than the approval one. **Dev has since been re-parsed and
rebuilt too** (issue 4's `reparse_dev_pilot --commit`, then the offline rebuild), so the two
environments now agree on parse and topology. The `competitor_candidate` table is created by
`init_db` on first start; no other migration.

| Phase | Size | Status |
| --- | ---: | --- |
| 1 — Multi-TA + indication model | 2.0 dw | **Done** |
| 2 — Canonical evidence schema | 1.5 dw | **Done** |
| 3A — Shared framework + extraction pipeline | 4.0 dw | **~3.0 dw done** — governance core, ingestion, offline re-parse, **+ the three agents, the baseline and the harness that decides between them** |
| 3B — Clinical evidence adapters | 2.2 dw | **Done** |
| 0 — Coverage + feasibility audit | 1.0 dw | **Done** — run live; see `PHASE0_COVERAGE.md` |
| X1 — Medical + statistical review surface | 1.5 dw | **Done** — Phase 7 unblocked; **+ curator study-verification surface**, the gate that blocks all output |
| 4 — Published synthesis adapter | 2.8 dw | **Done** — Level 2 now gates ahead of the engines |
| 6 — Resolver + engines + protocol | 6.0 dw | **Done** — **sidecar built and parity-verified** (`Dockerfile.nma`, `nma-sidecar/plumber.R`); **stop-and-review gate #4 closed**, worst delta ~1e-15 |
| — Ingestion + network construction | 0.5 dw | **Done** — not in the original estimate; Phase 6 had no data path |
| — First live resolve on real data | 0.3 dw | **Done** — engine holds; exposed 5 open issues, **2-5 now closed**, 3 new ones opened, **6 and 7 closed** |
| 5 — Competitor discovery | 2.3 dw | **Tier A done** — 47 tests; **swept live on prod's 37-study corpus**, which found issue 8 |
| 7 — Question generation | 2.3 dw | **Done** — 39 tests; generation is deterministic, and a gap is attributed before it becomes a question |
| 8 — Claim-level AI-vs-evidence evaluation | 4.3 dw | **Done** — 46 tests; the model observes, Python judges. Alignment dashboard at `/evidence/alignment` |
| 9 — Synthesis + recommendations | 1.5 dw | **Done** — 26 tests; the engine refuses to answer a curation backlog with content, and the UI says so |
| X2 — UI consolidation into `/evidence` | 0.5 dw | **Done** — **8 routes**, 18 tests, 8 pages under one tab; the three dead backend surfaces now render |
| — Evidence ingestion from the UI | 1.2 dw | **Done** — 5 routes, 24 tests; the three CLI scripts as background jobs with a preview-or-commit choice, so growing the corpus no longer needs a shell in the prod container |
| — Recording a decision from the UI | 0.4 dw | **Done** — **no new routes and no new tests**; the last two governance gates had backends and no button, so every result was `EXPLORATORY` by default rather than by judgement |

## Closing the gaps — what this session changed

Six areas where an endpoint existed and nothing reached it, or a plan gate existed and
nothing ran it. Each entry says what is now true and what is still not.

### Drug facts end to end

`DrugFact.verification_status` had no route that could change it, so it never left
`EXTRACTED` and every consumer filtering on `VERIFIED` returned nothing. That reads as *"no
findings"* rather than *"not wired"*, which is why it survived three phases: Phase 7's
approval and safety questions, Phase 8's approval, safety-warning and mechanism claims, and
Phase 9's misinformation-risk implication were all silently empty.

Now: a curation queue, a source-check that re-derives from the retained label, and a
curator-check route — plus `/evidence/drug-facts` to drive them. The queue separates *worth
verifying* from *cannot be fixed by verifying*: a label whose indications prose was never
structured cannot answer an approval claim however carefully it is checked. That is
extraction pipeline work, filed as such rather than as curator backlog.

**Still true:** the source-check compares stored fields against a re-fetch of the same
source. It cannot tell you the source itself is wrong.

### Network membership

Lifecycle 2 was defined and unreachable. The route now exists and
`/evidence/governance` drives it — deliberately **not** `/evidence/networks`, because a
lifecycle transition must not sit one click from a browse action.

The preview leads with the consequence: **with nothing INCLUDED, membership narrows
nothing**, and the first inclusion binds the filter so every other study stops
contributing. On today's corpus that is a cliff, and it is shown before the decision rather
than discovered afterwards from a study count that quietly dropped.

### SENSITIVITY_REQUIRED

The protocol could demand a second analysis and nothing computed one. It does now, and the
honest case is the one worth reading: when every path between two treatments crosses the
restricted route, the sensitivity analysis is **NOT_ESTIMABLE**, and that is the finding —
it says the comparison rests entirely on the link the policy is worried about. Divergence is
reported, never used to suppress the primary result.

### The netmeta sidecar

`Dockerfile.nma` and `nma-sidecar/plumber.R` exist, with pinned R, plumber and netmeta
versions, arm-level transport (so multi-arm correlation survives the wire), a refusal to
default `effect_measure`, and a contract-version check. `scripts/ec2_deploy.sh` builds it
**only when its own content hash changes**, so an application deploy does not reinstall the
R stack.

**On by default**, and the first cut of this was wrong. It shipped `NMA_SIDECAR=0` with a
comment reasoning that Bucher "covers every star network", taken from this document's own
description of the corpus. Measured instead of assumed:

```text
NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16
  rule NETMETA_IF_LOOPS_OR_MULTI_ARM_ELSE_BUCHER
  loop_count 1   independent_loops 0   has_multi_arm True (NCT03895203)
  ENGINE -> NETMETA
```

The only real network selects netmeta for **every** pair, so sidecar-off meant nothing
resolved at Level 3 at all. Worth keeping as a cautionary note: the error came from reading
a summary phrase in a progress doc rather than the topology, and the fix was a query that
took a minute.

A sidecar failure is **non-fatal to the application deploy**. It sits before the container
swap under `set -e`, so an unguarded failure would abandon a deploy of questions, scoring
and digests because CRAN was briefly unreachable. It now warns, deploys without it, and says
`DEGRADED` again at the end so the warning cannot scroll past. That is safe precisely
because `NMA_SERVICE_UNAVAILABLE` is a retryable service status that cannot be mistaken for
a finding about the evidence — and `NMA_SIDECAR_URL` is injected only when the sidecar
actually answered, so the configuration never claims more than is true.

### Stop-and-review gate #4 — **CLOSED**, on the EC2 box

Both halves green, and it closed **before** the first study is verified, which was the
ordering the risk note demanded.

```text
Woods2010 (COPD exacerbations)   6 cells   worst log delta 2.33e-15   OK
Dong2013  (COPD mortality)      15 cells   worst log delta 3.69e-15   OK
GOLDEN PARITY: PASS

tests/test_nma_parity.py         7 passed  (previously skipped — no sidecar)
```

Reproduce on the host with `docker exec evidence-nma-sidecar R -q -f
/app/golden/verify_golden.R`, or locally with `docker build -f Dockerfile.nma`, `docker run
-p 8100:8000`, then `NMA_SIDECAR_URL=http://127.0.0.1:8100 python -m pytest
tests/test_nma_parity.py -q`.

**The deltas are the informative part.** ~1e-15 is double-precision epsilon — the sidecar is
not merely *close* to `netmeta()`, it is doing the identical arithmetic and losing nothing in
transport. That is what retires the specific risk of this design: the JSON round-trip, the
arm-frame construction, the `pairwise()` conversion, the matrix extraction and the log/exp
scale handling. Had the `digits = NA` serializer fix not landed, these would have sat around
1e-4 and the gate would have said so.

Dong2013 is the one that mattered. It is the dataset that failed on the first run, and
`allstudies = TRUE` got it through `pairwise()` for the first time — 15 cells is the complete
six-treatment league table, so the multi-arm variance handling is exercised, not skirted.

**What it still does not cover**, unchanged and not to be quoted past: it does not
independently validate `netmeta` itself, it does not compare against the values *printed* in
the JSS paper (a transcription for a statistical reviewer, not something to generate from the
package under test), and Senn2013 stays out because contrast-level transport is unimplemented
on both sides of the wire.

**A third defect surfaced from the passing run, which the gate could not have caught.** The
output carried four `comb.fixed`/`comb.random` deprecation warnings, and `league_table` and
`net_split` were deciding fixed-vs-random by reading `net$comb.random` back off the fitted
object. Once netmeta removes the deprecated field that reads `NULL`, `identical(NULL, TRUE)`
is `FALSE`, and the sidecar would have served **fixed-effect estimates for every
random-effects request** — narrower intervals, no error, straight into the league table and
the alignment dashboard as unearned confidence. The version pin delayed that; it did not
prevent it. Both functions now take the requested model as an argument, since the caller
always knew it.

The gate passed *with that bug present*, because every golden case requested `random` — it
could not distinguish the two models at all. A fixed-effect Woods2010 case now guards the
switch, so serving the wrong model fails on tolerance instead of agreeing by luck. **Both
changes need a sidecar rebuild before the next gate run.**

### The 3A extraction pipeline

`extraction.py` defined the contracts and referenced a `run_baseline` that did not exist, so
the plan's documented descope had no artefact behind it. Now: three sequential agents
(extract → propose → validate), the single-call baseline, a `baseline_with_validation`
runner, and `app/evidence/harness.py`, which measures them against a hand-labelled corpus
and returns a verdict.

The harness counts a **miss apart from a wrong answer** (an abstainer must not rank level
with a confident fabricator), reports accuracy **per licence tier**, and **ships the
baseline on a tie** — equal accuracy for three model calls instead of one is a cost with no
return.

**Not run.** `python -m scripts.extraction_harness` costs real model calls and has not been
executed, so **no claim is made here about whether the agents beat the baseline**. The
corpus is three cases; that is enough to make the verdict mechanical, not enough to settle
it. Widening it is curator time.

### The three dead frontend surfaces

`/comparisons/matrix`, `/evidence/drug-facts` and the Phase 9 synthesis endpoint each had a
tested backend and no page. All three now render, at `/evidence/comparisons`,
`/evidence/drug-facts` and `/evidence/synthesis`. The comparison view shows the **whole
fall-through chain**, not just where the walk stopped, so a row can say "a head-to-head
trial exists but reports week 12, so this is indirect". The synthesis view puts limitations
**above** the findings.

That takes the evidence sub-nav to **nine pills**, which is the width at which the
top-level header already overflows — see the note under X2. Adding these three was right;
adding a tenth would not be.

### Line endings

`.gitattributes` forced LF on `*.sh` but matched `Dockerfile` as an exact name only, so the
new `Dockerfile.nma` and the two `.R` files were covered by nothing and were LF only by
luck. A CRLF Dockerfile appends `\r` to every `RUN`, which would have surfaced as a
confusing CRAN failure rather than a line-ending one. `Dockerfile.*` and `*.R` are now
pinned to LF.

### Drug facts, ingested in production

`scripts.ingest_drug_facts --commit` ran on the box: **4 labels ingested**, all landing
`MAPPED` and awaiting a curator, which is the correct default — ingestion never verifies its
own output.

| Brand | Fact id | Label updated | Flags |
| --- | --- | --- | --- |
| Humira | `DF-HUMIRA-2025-12-23` | 2025-12-23 | indications not structured |
| Skyrizi | `DF-SKYRIZI-2026-06-26` | 2026-06-26 | **drug class conflicts with curated**, indications not structured |
| Rinvoq | `DF-RINVOQ-2026-06-30` | 2026-06-30 | indications not structured |
| Tremfya | `DF-TREMFYA-2026-06-04` | 2026-06-04 | **drug class conflicts with curated**, indications not structured |

**`DRUG_CLASS_CONFLICTS_WITH_CURATED` on Skyrizi and Tremfya is worth a look before anyone
curator-checks them.** Both are IL-23 inhibitors, and both disagreeing the same way points
at `brands.yaml` rather than at two independent label quirks. A curator confirming the label
without resolving that would ratify a class the catalog contradicts. The flag is the
adjudication, not a warning to click past.

`INDICATIONS_TEXT_NOT_STRUCTURED` on all four is by design: the adapter records the fact
rather than half-parsing indications prose. Verification does not change it — structuring
indications is 3A pipeline work.

### Still open

- ~~**Close stop-and-review gate #4**~~ **done** — see above. Three real sidecar defects
  found and fixed along the way; the gate earned its place twice over.
- **Resolve the IL-23 class conflict** in `brands.yaml` before curator-checking Skyrizi or
  Tremfya.
- **Data + ops**: the backfill-script contradiction, a `WITHDRAWN` competitor-candidate
  state, and the Treatment Continuation blast radius are untouched.
- **The evidence sub-nav is full, and was appended to once anyway** — see the amendment under
  X2. Ten pills, wrapping. The next surface folds in.

## Blocked on someone other than an engineer

- ~~**EBS resize**~~ **done.** Grown 16 → 50 GiB gp3 and the filesystem extended on the
  host (`growpart /dev/xvda 1` then `resize2fs /dev/xvda1`; the device is **xvda**, not
  nvme). `/` now ~49G with ~44G free, clearing Phase 6's ~8–10 GB sidecar peak.
- **Legal sign-off** on the licence-retention matrix in `app/evidence/licensing.py`
  before restricted-source ingestion goes live. The matrix is a conservative engineering
  default, not a legal determination.
- **Named medical and statistical approvers.** The surface now exists (X1) and enforces two
  independent roles, but `POST /evidence-review/protocols/{id}/decisions` needs real people
  behind `reviewer_id` — and **that field is recorded, not authenticated**, because RBAC is
  absent from this tree. Everything ships without them; Level-3 results stay `EXPLORATORY`.

## Phase 1 — done

Root causes 1–5 and 8–9 from the plan, all verified in code before changing anything.
`brands.yaml` gained an additive `indications:` overlay (8 diseases), a `drug_catalog:`
of 13 agents, and `drug_class`/`administration_route`/`evidence_depth` annotations.
`map_query` resolves `disease` on a **second, independent pass** so the four legacy keys
stay byte-identical — pinned by `MAP_QUERY_SNAPSHOT` in `tests/test_multi_ta.py`.

Two decisions worth not re-litigating:

- **`evidence_depth` defaults to `standard` for focus brands too.** Defaulting them to
  `full` swept Lupron, Vraylar and the GLP-1 set into the evidence programme. Scope is a
  decision, not a side effect of being a focus brand. `full_depth_drugs()` is pinned to
  exactly `{Rinvoq, Skyrizi, Humira, Tremfya}`.
- **Tremfya stays in `competitors:`.** Promoting it to `focus_brands` would make
  `alert_engine` skip it and silently kill `COMPETITOR_ADVANTAGE` — commercially the most
  valuable alert in this space. Regression test guards it.

**Not yet applied:** `scripts/backfill_question_disease.py` has only been dry-run
(358 questions scanned, 57 resolvable, 301 correctly left NULL). Run with `--commit`.
`--fix-therapeutic-area` is opt-in and off by default because rewriting a stored TA moves
historical rows between live dashboard filters.

## Phase 2 — done

8 tables, all registered in `init_db`: `source_payloads`, `drug_facts`,
`clinical_studies`, `study_arms`, `outcome_results`, `evidence_networks`,
`network_memberships`, `nma_results`.

- `app/config/canonical_outcomes.yaml` — 20 endpoint IDs + 6 population strata. The
  single owner of endpoint semantics; `brands.yaml` only ever references these IDs, and a
  dangling reference is a **fatal** startup error (`main._validate_configuration`).
- `app/evidence/licensing.py` — retention follows the **licence class**, never the
  acquisition route. `enforce()` is subtractive by construction, so a RESTRICTED source
  physically cannot end up holding a document. Unknown sources default to RESTRICTED.
- `app/evidence/lifecycles.py` — the three lifecycles are separate because a study can be
  VERIFIED, INCLUDED in ACR50 and EXCLUDED from ACR20 simultaneously. `RATIFIED` is
  reachable only through both review stages **in order**; excluding a study without a
  reason raises.
- `app/evidence/statuses.py` — every resolver path ends at a named status, including
  every failure. `is_releasable()` is the single predicate that keeps exploratory results
  out of downstream consumers.

## Phase 3A — governance core + the agents and their harness done; curation UI pending

**Landed:**

- `app/evidence/sources/base.py` — `SourceAdapter` protocol + `get_json` never-raises
  boundary, mirroring the proven `geo/sources/openfda.py` contract. `parse` is pure and
  synchronous, which is what lets 3B's tests run with no network.
- `app/evidence/extraction.py` — the proposal-only constraint, enforced **structurally**:
  `HarmonisationProposal` is frozen, has no `apply()`, and shares no field name with
  `OutcomeResult` (a test asserts the namespaces stay disjoint — `rejection_reason`
  originally collided, meaning two different things on either side of the boundary).
  A proposal outside the protocol's `approved_time_window` is **auto-rejected with no
  escalation path**; escalating would invite someone to overrule an approved protocol
  from a review queue. A validation disagreement blocks promotion to VERIFIED outright —
  not a confidence penalty.

- `app/services/evidence_ingestion_service.py` + `app/services/network_builder_service.py`
  — the missing data path. `clinicaltrials.parse` returned unsaved rows and pointed at an
  "ingestion service" that did not exist, so until this landed the only code persisting a
  `ClinicalStudy` was a test file and `/comparisons/resolve` 404'd on every call in
  production. Phase 6 was a fully tested engine with no fuel.

  Four decisions worth not re-litigating:

  - **Ingestion never verifies its own output.** Rows land `EXTRACTED`, or `MAPPED` when
    every endpoint resolved to a canonical id — that one is a statement of fact, not a
    judgement about accuracy. `EXTRACTED -> VERIFIED` is not even a legal transition. A
    pipeline that self-certified would make the verification lifecycle decorative and the
    resolver's refusal to compute on unverified rows meaningless.
  - **A decided study is never overwritten.** Re-harvesting a `VERIFIED` or `REJECTED`
    study is reported as skipped; a correction must create a new version. Undecided rows
    are replaced freely, since there is no history to protect yet.
  - **The builder proposes, never includes.** Memberships are `PROPOSED` and the network is
    `DRAFT`. Rebuilding preserves an existing `INCLUDED`/`EXCLUDED` decision, because the
    builder re-running is not new information about a judgement someone already made.
    Rebuilding a `RATIFIED` network raises rather than silently changing the evidence set
    under a review it already passed.
  - **Topology is not reimplemented.** `evidence/topology.py` already served the Phase 0
    audit and Phase 6 engine selection; a third implementation would eventually disagree
    about whether a network has a closed loop, which would mean promising a comparison the
    resolver then refuses to compute. A within-study triangle is correctly counted as 1
    loop but **0 independent loops**.

  `scripts/ingest_evidence.py` runs it end to end, dry-run by default. `--verify-as NAME`
  is opt-in and prints that the name is **recorded, not authenticated**. Without it studies
  stay unverified and nothing resolves, which is the correct default.

  The runner reports `uncurated treatment labels` as a first-class figure, because Phase 0
  measured 12-20% catalog coverage and those labels become junk nodes in every network
  built from the indication.

- `app/evidence/agents.py` + `app/evidence/harness.py` — the three sequential agents
  (extract → propose → validate), the single-call `run_baseline` that `extraction.py`'s own
  docstring already referenced but which did not exist, a `baseline_with_validation` runner
  so the documented descope is a config change rather than a rewrite, and the harness that
  decides between them on a hand-labelled corpus.

  The harness counts a **miss apart from a wrong answer** (an abstainer must not rank level
  with a confident fabricator), reports accuracy **per licence tier** (a fragment-only
  source cannot be re-derived in full, so one headline number would overstate coverage), and
  **ships the baseline on a tie**.

**Still to build in 3A:** LLM mapping framework, curation API routes, curation UI against
fixtures. (The drug-fact curation surface landed — see *Closing the gaps* — but that is the
label side, not the extraction-proposal queue this line means.)

> Build the framework + single-call baseline first, then add agent stages behind the same
> interface. That keeps 3B/4 unblocked at ~2.5 dw and makes the agent work independently
> descopable. **If the pipeline cannot beat the baseline on the same fixture corpus, ship
> the baseline plus the validation stage.** Agent count is not a quality metric.

**Run on 2026-07-27, against live model calls. The baseline won.**

```text
baseline   85.7% (12/14)   2 wrong, 0 missed   PUBLIC_DOMAIN 81.8%  RESTRICTED 100.0%
pipeline   71.4% (10/14)   3 wrong, 1 missed   PUBLIC_DOMAIN 72.7%  RESTRICTED  66.7%
VERDICT    SHIP_BASELINE_PLUS_VALIDATION
```

The three-stage pipeline was **both wronger and abstaining more** — the extra stages added
noise, not signal. The pre-committed rule (ship the pipeline only if strictly more accurate)
therefore selects the documented descope. Recorded as `agents.DEFAULT_RUNNER`, pinned by
`test_the_default_runner_is_the_measured_descope_not_the_agent_pipeline`.

**What this does and does not establish.** It is 14 graded fields over 3 cases and the gap
is 2 fields — nowhere near significant, and the per-tier splits are single observations.
It should not be quoted as "agents are worse than a single call". What makes the verdict
safe anyway is the *direction* of the tie-break: the rule already ships the simpler, cheaper
runner unless the complex one earns its place, so a corpus too small to settle the question
resolves the same way as a genuine tie. Widening the corpus is curator time.

**Nothing in `app/` calls a runner yet** — deterministic parsing in
`evidence_ingestion_service` is what populates rows today. The constant exists so the first
caller inherits the measured decision rather than picking a runner fresh.

## Phase 3B — done

All three adapters, every test offline against committed fixtures.

- `app/evidence/endpoints.py` — shared endpoint matcher. Two adapters resolving "ACR50 at
  week 16" differently would build two incompatible networks from the same evidence, so
  there is one matcher. **Ambiguity is never resolved by guessing**: a title naming both
  ACR20 and ACR50 returns no match plus the candidates. The timepoint window is a filter,
  not a tiebreak — it is what separates UC induction remission (weeks 8-12) from
  maintenance remission (weeks 44-60), two endpoints with identical wording.
  Vocabulary lives in `canonical_outcomes.yaml` as `match_tokens`, so adding a synonym is
  a reviewed config change.
- `app/evidence/sources/clinicaltrials.py` — the `outcomeMeasures -> classes ->
  categories -> measurements` parser. Four awkward realities handled explicitly: results
  groups are not protocol arms (reconciled on title); binary results are posted as
  rounded percentages so back-derived counts are flagged `EVENTS_DERIVED_FROM_PERCENTAGE`;
  stratified outcomes yield one row per class rather than collapsing to the first; an
  unrecognised `paramType` warns rather than guessing. IDs are deterministic, so
  re-ingestion updates instead of duplicating.
- `app/evidence/sources/openfda_facts.py` — label to `DrugFact`. **The curated table wins**
  on class and route; a genuine disagreement is flagged for review, a wording difference
  ("JAK inhibitor" vs "Janus Kinase Inhibitor") is not. Nothing reaches VERIFIED —
  structured is not the same as understood.
- `app/evidence/sources/pubmed.py` — citations, plus the licence boundary. Only true
  NMAs/ITCs reach the Level-2 queue; a pairwise meta-analysis cannot resolve an indirect
  comparison.

## Phase 0 — done, run live

Results in `docs/PHASE0_COVERAGE.md`. Re-run with:

```bash
python -m scripts.evidence_coverage_audit --all --out ../docs/PHASE0_COVERAGE.md
```

- `app/evidence/topology.py` — shared by the audit and Phase 6 method selection, so the
  audit can never promise a comparison the resolver then refuses. **`loop_count` vs
  `independent_loop_count` is the distinction to preserve**: a three-arm trial forms a
  graph triangle, but its comparisons share a control group and cannot test
  inconsistency.

### The first run was invalid — what it exposed

It reported `L3 feasible: NO` for all eight indications. That was an artifact of four
defects, not a finding. Kept here because the same mistakes are easy to reintroduce.

1. **Whole-graph connectivity asked the wrong question.** PsA reported all six pairs
   `DIRECT` and `Connected: no` simultaneously. Searches sweep in unrelated agents that
   form isolated islands. Now judged on the **focus-drug component**.
2. **Node inflation from uncurated arm labels.** Curated drugs collapsed to a dose-free
   node while uncurated ones kept the full title, so every dose variant became its own
   node — PsA showed 91 nodes for an ~20-node network. Fixed in the adapter; dose still
   lives in `dose_value`. PsA is now 54.
3. **Observational studies entered an RCT network.** No study-type filter, so registry
   cohorts became large cliques of non-randomised "comparisons". PsA's independent loop
   count fell **15 → 1** once interventional+randomised filtering was applied.
4. **`treatment_phase` was never set by the parser**, so the Tier-3 gate was reading a
   column default. Now inferred from trial titles; a trial naming both phases is left
   PRIMARY and counted separately rather than assigned to one.

A fifth defect surfaced only because the placebo field stayed empty after those fixes:
**registry results carry two independent group ID spaces** — participant-flow `FG###` and
each outcome measure's own `OG###`. The parser looked measurement IDs up in the
participant-flow map, so on real data *every* outcome row came back unattached to an arm.
The fixture had reused `FG###` in both places and hid it. Now reconciled on group title,
with an unmatched title flagged rather than guessed.

**That fix was only half of it, and the other half went unnoticed for two phases.** Title
reconciliation still lost 664 rows (10.5%) on live data, because the two ID spaces also
describe two different *partitions* of the same patients — see "Arm defects the first live
resolve exposed" below.

### Findings that should drive sequencing

- **Psoriatic Arthritis is the strongest network and the only one containing all four
  focus drugs.** Rinvoq vs Humira is DIRECT; Skyrizi and Tremfya connect via Humira and
  placebo. This confirms the plan's PsA priority on evidence rather than assumption.
  **Qualified since:** direct at the *endpoint* level only. Under the approved
  `PSA_ACR50_W16_PRIMARY` protocol the same pair resolves `NETWORK_DISCONNECTED`, because
  the head-to-head trial reports ACR50 at week 12 and the protocol admits 14-18. Issue 1
  below.
- **Route-mixing is a measured problem, not a theoretical one.** Where both routes were
  observable the placebo spread exceeded the 5pp threshold: UC maintenance W52 ORAL 18.8%
  vs SC 5.1% (13.7pp), CD endoscopic response W12 ORAL 3.5% vs SC 12.8% (9.3pp). Rinvoq
  is oral and the comparators are injectable, so this bears directly on the headline
  comparisons. **Read it cautiously**: n is small (CD ORAL n=1) and the direction reverses
  between UC and CD, which points to confounding by agent and trial rather than a clean
  route effect. It justifies `SENSITIVITY_REQUIRED`, not a claim about routes.
- **Atopic Dermatitis and nr-axSpA are not Level-3 feasible** — only one focus drug each.
  Correct given the drug set, but the AD comparison anyone actually wants is Rinvoq vs
  Dupixent, which the audit never tested because Dupixent is not a full-depth drug. A
  scope question, not a data gap.
- **UC and CD induction/maintenance are separable**, but 7 and 8 trials respectively name
  both phases in one registration and must be split into substudies first.

### Remaining caveat

**Catalog coverage is only 12-26% of nodes.** Node and loop counts remain an upper bound
until more comparators are curated into `drug_catalog`. Connectivity findings for the
focus drugs are unaffected — those nodes are all curated.

## X1 — medical + statistical review surface — done

Gates Phase 7. Protocol approval is what turns an `EXPLORATORY` result into a `GOVERNED`
one, and Phase 7 may only generate approved questions from `GOVERNED` results.

**Not to be confused with BR-013.** That findings doc describes a *pharmacovigilance
triage queue* for flagged AI responses and social posts — a different domain with a
different data model. X1 is evidence governance. BR-013 remains unimplemented.

- `config/analysis_protocols.yaml` — four protocols spanning the policy space: PsO
  (single-route), RA and PsA (route-mixed but **unmeasured**), UC maintenance
  (route spread **measured** at 13.7pp). Methodology only.
- `app/evidence/protocols.py` — loader + **derived** `content_hash` + `validate()`.
- `app/evidence/approvals.py` — pure precedence rules over approval rows. No DB import,
  so the governance logic is testable without a session.
- `app/models/analysis_protocol.py` — `AnalysisProtocolApproval` only. The definition
  stays in YAML.
- `app/services/evidence_review_service.py` — decisions, revocation, the two ratification
  stages, and `governance_gate`.
- `app/api/evidence_review.py` — 9 routes under `/evidence-review`.

### The four rules that make this coherent

1. **`content_hash` is derived, never accepted as input.** No function or route takes a
   hash parameter. A client that could name the content it approves could sign off on
   something other than what is on disk.
2. **Approval state lives outside the hashed content.** If it lived in the YAML, recording
   an approval would change the content, change the hash, and invalidate the approval just
   granted. `FORBIDDEN_KEYS` makes authoring it a startup failure.
3. **Editing a definition invalidates prior approvals automatically.** Nothing revokes
   anything — the stored hash simply stops matching and the status derives as
   `SUPERSEDED`. Invalidation cannot be forgotten because nobody performs it.
4. **A rejection cannot be outvoted.** One role rejecting is decisive; the precedence order
   in `derived_status` puts `REJECTED` first so an approval from the other role cannot
   overrule it.

### Two things worth knowing

**The window check caught a real error in my own protocol file** on first run: the PsO
protocol allowed weeks 14-18 while `PSO_PASI90_W16` allows only 12-16. A protocol may
*narrow* the outcome's window under statistical judgement but never widen it — widening
would silently admit results the endpoint definition itself rejects.

**The hash tracks meaning, not layout.** Whitespace inside strings is collapsed before
hashing, because YAML folded scalars (`>-`) have already discarded the author's line breaks
by the time the loader sees them — re-wrapping a long `estimand` is indistinguishable from
the original after parsing, so it must not retire an approval. Changing the words does.

### Limitation to state plainly

**`reviewer_id` is recorded but not authenticated.** RBAC was removed from this tree, so
the audit trail says who *claimed* to act, not who provably did. The approval model and its
invariants are testable today and enforcement attaches to these same routes once roles
return — but until then this is a governance record, not a security control.

## Phase 4 — published synthesis adapter — done

Level 2 outranks anything we compute, so it is checked **before** the engines rather than
beside them. Landed:

- `app/evidence/treatments.py` — the shared treatment-label normaliser, **extracted from**
  `sources/clinicaltrials.py` (which now imports it). Same reasoning as `endpoints.py`: if
  the registry adapter calls a node `Rinvoq` and the published adapter calls it
  `Upadacitinib`, a published league table and an internal network appear disjoint and the
  overlap check silently passes when it should fail. The registry's regression guards were
  left pointing at the same function so they still cover the moved code.
- `app/evidence/sources/published_nma.py` — league table, SUCRA/P-score, GRADE,
  heterogeneity and inconsistency, normalised out of whichever shape the source used:
  triangular matrix, contrast list, or effects against a single common reference; interval
  as `ci`/`crl`/`lower_cri`/`[lo, hi]` or printed inline as `1.40 (1.10 to 1.80)`.
- `app/evidence/suitability.py` — the Level-2 gate.
- `app/services/published_synthesis_service.py` + `app/api/published_synthesis.py` — the
  governed upload path, 3 routes under `/published-syntheses`.
- `statuses.PUBLISHED_SYNTHESIS_UNSUITABLE` — new, the Level-2 counterpart to
  `DIRECT_EVIDENCE_UNSUITABLE`. "No published synthesis covers these treatments" and "one
  does, but it does not fit" are different findings, and only the second tells a reviewer
  there is a paper worth reading.

### The rule the phase exists to enforce

**An NMA containing both Rinvoq and Tremfya can still be unsuitable.** Containing both
treatments is necessary and nowhere near sufficient. `assess()` runs every check and
reports **every** failure rather than short-circuiting, because a reviewer deciding whether
to chase a paper down needs to know it missed on both timepoint and population. Nine
dimensions: indication, both-treatments-present, phase, endpoint, timepoint (against the
protocol's approved window), population stratum, recoverable included studies, recency, and
an actual published estimate for that exact pair.

That last one is its own case: both nodes can be in the network while the league table never
reports them against each other.

### Four things the adapter refuses to do

Each would misrepresent the source, so each is a flag rather than a fill:

1. **Never conflate a CrI with a CI.** `interval_type` is preserved and flagged
   `INTERVAL_TYPE_NOT_STATED` when the record does not say. A Bayesian credible interval
   and a frequentist confidence interval support different statements.
2. **Never infer the effect measure from magnitude.** An estimate near 1.0 could be a risk
   ratio or an odds ratio and guessing inverts conclusions. Reading `rr` off a *key name*
   is allowed — that is the source naming the measure structurally, not a guess.
3. **Never invent a missing interval.** An estimate with no interval stays intervalless.
4. **Never silently reverse a contrast.** `1.4` for Rinvoq-vs-Humira and for
   Humira-vs-Rinvoq are reciprocals, so direction is preserved and a reversal is *disclosed*
   in the decision text.

Ranking scores are the one transformation: values above 1 are rescaled from percent, which
is determinate rather than a guess (neither SUCRA nor a P-score exceeds 1 as a proportion)
and is flagged `RANKING_SCORES_RESCALED_FROM_PERCENT` so the stored number can be
reconciled against the printed table.

### Two bugs the tests caught

- **`"sucra": null` shadowed a real `p_score`.** The metric loop matched on key presence,
  so an extractor emitting an explicit null for the metric a paper *didn't* report
  discarded the one it did. Now skips null-valued keys.
- **Check order reported a symptom ahead of its cause.** Endpoint resolution is scoped by
  phase, so a synthesis with the wrong phase also fails to resolve its endpoint — and
  `ENDPOINT_MISMATCH` was surfacing instead of `TREATMENT_PHASE_MISMATCH`. Phase is now
  checked first.

### A fresh upload is not marked `PUBLISHED_RESULT_AVAILABLE`

That status asserts a paper *passed suitability*, and at upload time no question has been
asked and no reviewer has checked the extraction. Uploads store as
`MEDICAL_REVIEW_REQUIRED` — accurate: a real paper, an unreviewed reading of it. Suitability
is judged **per question, on demand**, never cached onto the row, for the same reason
network membership is scoped per network-and-protocol rather than being a study column.

Similarly `source_is_citable=True` but `claim_is_approved_for_external_use=False`. The
article's authority does not transfer to our unverified extraction of it.

### Retention is enforced, not requested

Every upload routes through `SourcePayload.record`, so the licence decision happens inside
the model constructor and this service has **no code path that bypasses it**. Uploading a
paywalled Cochrane PDF yields the extracted values, citation, checksum and page provenance
— and no document, with `dropped_fields` recording why. The response reports the drop so a
reviewer can tell a licence decision from a failed upload. Re-upload deduplicates on the
content checksum, since manual upload has a submit button.

### Two limitations to state plainly

**This adapter does not read PDFs.** Its input is a *normalised extraction record*; the
PDF-and-table to record step is 3A's remaining LLM pipeline work. That is why `parse` is
pure and every test runs offline, and it is also why an unreviewed upload cannot be trusted
as evidence yet.

**Recency is a proxy.** What actually matters is whether a synthesis includes the current
evidence base, which is a study-overlap check against an internal network — only possible
in Phase 6, where a network exists to compare against. `max_age_years` is therefore an
explicit caller-supplied gate (default 5, disableable) rather than a number dressed up as
more principled than it is.

## Phase 6 — resolver + engines + protocol — done

The hierarchy is now executable. `GET /comparisons/resolve` walks L1 → L2 → L3 → L4 and
**always** returns a named status; an unanswerable comparison is a 200 carrying a structured
gap, never a 4xx.

- `app/evidence/engines/pairwise.py` — effect measures, inverse-variance pooling,
  DerSimonian-Laird random effects, zero-event policies. Pure and exact.
- `app/evidence/engines/bucher.py` — the adjusted indirect comparison.
- `app/evidence/engines/netmeta.py` — **wire contract** to the R `netmeta` sidecar plus a
  never-raises client, mirroring `sources/base.get_json`.
- `app/evidence/resolver.py` — the walk. Pure, no DB import, like `approvals.py`.
- `app/services/comparison_service.py` + `app/api/comparisons.py` — scoping from real rows,
  the governance gate, persistence, 3 routes under `/comparisons`.
- `settings.nma_sidecar_url` — **blank by default**, and that is a supported state.

### Ratios are pooled on the log scale

Averaging risk ratios directly is wrong, and wrong in a way that looks fine: RR 0.5 and
RR 2.0 are equal and opposite effects whose arithmetic mean is **1.25**, so a naive pooling
reports a 25% harm where the evidence is perfectly balanced. The analysis scale is therefore
log for RR/OR/HR and the identity for RD/MD/SMD, converted back only for display. It is also
what makes Bucher's subtraction valid — `d_AB = d_AC − d_BC` holds on the log scale and
nowhere else.

### Four places the answer is "not estimable" rather than a number

1. **A single study has no estimable heterogeneity.** `i_squared` is `None`, never 0 —
   zero would assert that homogeneity was assessed and confirmed. Consequently
   `RANDOM_EFFECTS_IF_I2_ABOVE_50` selects *random* effects when I² is unknown, because a
   single study cannot demonstrate homogeneity.
2. **A double-zero study carries no information about a ratio.** Excluded, and the exclusion
   recorded. Correcting it would manufacture an effect of exactly 1.0 with a finite interval
   out of a study that observed nothing.
3. **A continuity correction is a change to the data**, so every corrected study is flagged.
   Which correction applies is the protocol's `zero_event_policy`, never a default chosen in
   the engine.
4. **Multiple Bucher anchors are never averaged.** Two anchors give two estimates, and if
   their intervals do not overlap that disagreement *is* the finding — evidence against
   transitivity. Averaging would hide it.

### The Sweeting correction was backwards on first write

Worth recording because the balanced case hides it. Sweeting's correction is *proportional to
the reciprocal of the opposite arm's size*, which after normalising works out as proportional
to the arm's **own** size: `c_i = n_i / (n_1 + n_2)`. That shifts both arms' observed risk by
the same `1/(n_1+n_2)` and so leaves the ratio undistorted. I first implemented it inverted,
which for n=50 vs n=150 perturbed the arms 9:1 — **worse than the flat 0.5 it replaces** (3:1).
For balanced arms all three forms coincide at 0.5, so only the unbalanced test catches it.
`test_the_correction_perturbs_both_arms_equally_when_arms_are_unbalanced` pins it.

### Why netmeta is a contract and not an algorithm

A graph-theoretic NMA is not hard to code and is very hard to code *correctly* — multi-arm
correlation, net-splitting, SUCRA by simulation. `netmeta` is validated, cited in the HTA
submissions our numbers will be compared against, and reports a package version a
statistical reviewer can check. Hand-rolling it would ask reviewers to trust our arithmetic
over a reference package for a figure that ends up in a promotional review.

Three properties of the contract:

- **Multi-arm structure survives the wire.** Arms are transmitted grouped by study and never
  flattened; three pairwise rows would double-count the shared control group.
- **The sidecar has no defaults of its own.** Every statistical choice is sent explicitly
  from the protocol, because a default applied on the far side of a wire is a methodology
  decision nobody approved and nobody can see.
- **An outage is a SERVICE status, never an evidence gap.** `NMA_SERVICE_UNAVAILABLE` means
  retry; a gap means this comparison is not estimable. Conflating them would let an
  infrastructure blip masquerade as a finding about the evidence. Bucher runs in-process, so
  a missing sidecar never degrades a comparison Bucher could have answered.

### Governance mapping, and why the engine status is carried separately

- fully governed (protocol approved **and** network ratified) → `GOVERNED_SYNTHESIS_COMPLETED`
- anything less → `EXPLORATORY_RESULT_COMPLETED`, which `is_releasable` excludes
- `BUCHER_ITC_COMPLETED` / `INTERNAL_NMA_COMPLETED` ride along on `engine_status`

The engine-specific statuses name the *method* and say nothing about governance, so using one
as the headline `status` would make an ungoverned result look releasable. Asking for GOVERNED
without an approved protocol **downgrades and records the blocking status** rather than
raising — the honest report is "computed, but not releasable, and here is what is missing",
not an error that hides the fact a number was obtainable.

### A pooled direct estimate is internal output

Caught by a test I had written wrong. One head-to-head trial's risk ratio belongs to the
trial that produced it. Pooling three of them is a meta-analysis **we** performed, and
labelling the two identically presents our synthesis as raw trial evidence. Level 1 now sets
`POOLED_ACROSS_MULTIPLE_STUDIES` and `is_internal_output` when more than one trial
contributed.

### Falling through is recorded, not forgotten

`considered` accumulates every level tried and why it was rejected. That is what lets an
answer say *"a Cochrane review covers both drugs but reports ACR20, and the head-to-head
trial used week 12, so this is an indirect estimate"* — three facts a reviewer needs, none of
which survive a resolver that only reports where it stopped. `GET /comparisons/evidence`
exposes the same scoping report on its own, because "why was my trial not used?" is the most
common curator question and answering it should not require running a computation.

### Two limitations that gate everything downstream

**The sidecar is not deployed.** `nma_sidecar_url` is blank, so any comparison whose protocol
selects a full NMA currently resolves to `NMA_SERVICE_UNAVAILABLE`. Every PsA pair in Phase 0
is a star or has direct evidence, so Bucher covers the headline comparisons today — but a
network with a closed loop or a multi-arm trial has no engine until the R service ships.

**Nothing is releasable yet.** No protocol has an approval and no network is ratified, so
every computed result is `EXPLORATORY`. That is the designed state and is why Phase 6 could
be built and tested before any approver exists — but it means the numbers cannot flow to
Phase 7/8/9 until the named reviewers in the blocked list actually sign.

## Phase 7 — question generation — done

Generates monitoring questions from the evidence store, each carrying the answer the
evidence supports and the rows it rests on. `app/evidence/question_generation.py` (pure),
`app/models/question_evidence.py`, `app/services/evidence_question_service.py`,
`app/api/evidence_questions.py` (3 routes + a vocabulary route),
`tests/test_evidence_questions.py` (39 tests).

All seven categories from the plan: drug fact, comparative efficacy, population-specific,
safety, evidence quality, competitor discovery, evidence gap.

### Question text is constructed, not generated

`variations/generator.py` tells a model *"do NOT introduce any new facts, claims, drug
names, doses or comparisons"* — a rule the model is **asked** to follow. A template cannot
say anything its inputs do not, so the same rule is a property of the code here, the same
move `HarmonisationProposal` makes by being frozen with no `apply()`. It matters more here
than anywhere else in the programme: these questions are sent to external models and their
answers are then graded against our evidence, so a generated question that quietly
introduced a claim would seed the corpus with the very thing Phase 8 exists to detect. No
LLM call, no network, every category testable offline.

The **expected answer** quotes stored fields and never characterises them. Every flag the
evidence carries travels with it — an expected answer that dropped
`EVENTS_DERIVED_FROM_PERCENTAGE` would launder a caveat the extraction was careful to record.

### A gap is not automatically an evidence gap

The finding the phase turns on. `statuses.is_gap` is true for `NETWORK_DISCONNECTED`
**however the network came to be disconnected** — and in prod it is disconnected because
nothing is verified, so `gather_evidence` skips every study before topology is considered.
Generating *"no evidence compares these treatments"* from that states a fact about the world
on the strength of a fact about our own backlog, and puts a curation ticket into a monitored
corpus where nothing downstream can tell the two apart.

`attribute_gap(status, scoping)` returns `CURATION` / `PROTOCOL` / `EVIDENCE`, matched
against the scoping report's own rejection wording rather than reconstructed. Precedence is
`CURATION -> PROTOCOL -> EVIDENCE`, because that is the order the checks happen upstream: an
unverified study never reaches the window check, so a corpus with a verification backlog
cannot yet know whether the protocol would have excluded anything. **Only `EVIDENCE` becomes
a question.** The other two are returned as counts — `gaps_attributable_to_curation` and
`gaps_attributable_to_protocol` — because they name work for a curator and for issue 1's
reviewer respectively, and a silently shorter question list would look like thin evidence.

### A gap question asks the question a person asks

The plan's phrasing is *"why can Rinvoq and Drug X not be compared, and what evidence would
be required?"* — an internal research question. What goes to a model is the ordinary
comparative question, because **a model asserting superiority where no comparison is
estimable is precisely what this category exists to catch**, and it can only be caught by
asking what a real user asks. The expected answer says *"absence of evidence, not evidence of
equivalence"*; "what would be required" lives in `required_evidence`, named per status.

### A node set is not a question set — found on live data

The first live run generated *"is Bimzelx or Placebo more effective at achieving ACR50 at
week 16?"*. Placebo is in almost every network because every indirect estimate chains through
it, so without a screen the majority of generated questions are contrasts nobody asks — and
each one costs a real model call on every run. `is_monitorable_pair` screens placebo,
class-level and aggregate nodes, **delegating every judgement to `evidence.treatments`**
rather than restating it. On the dev PsA network: 12 questions -> **4**, with
`pairs_not_monitorable: 5`.

The surviving headline is worth reading: Bimzelx vs Humira resolves `RR 0.959 (0.778-1.18)`
and the expected answer is *"Neither is shown to be more effective: the interval includes no
difference."* The generated answer is never stronger than its interval, which matters because
Phase 8 grades hedging against it — an over-claiming expected answer would mark a correctly
cautious model wrong.

### Governance

- **Staged, never approved.** Generation writes `harvested_questions` rows as `CLASSIFIED`
  with `source="evidence"`, reusing the existing double gate rather than reimplementing it,
  so reviewers keep **one** queue. Promotion stays on `/harvest/items/{id}/promote`, which
  already owns the PII, injection and adverse-event guards — a second promotion path would be
  a second place for those to be forgotten.
- **Dry run by default**, and a dry run **adds nothing and rolls nothing back**. Deciding what
  would happen and then undoing it would leave the caller unable to *not* write — the inverse
  of issue 4's unconditional commit, and just as invisible.
- **`QuestionEvidence` carries one required, single-valued `relationship_role`.** Separate
  supports/contradicts/context booleans permit all three true at once; a single value makes
  the contradiction unrepresentable. A `UniqueConstraint` closes the same hole reached by
  inserting twice instead of setting two flags.
- **The reference is `(evidence_type, evidence_id)`, not six nullable FKs.** Evidence lives in
  six tables and one FK cannot span them; six nullable ones would reintroduce the nonsense
  states the role enum was chosen to forbid, one level down. The cost is stated rather than
  hidden: the database cannot enforce existence, so the service resolves every reference and
  `associations()` reports `exists: false` rather than silently treating it as unverified.
- **Associations are materialised at promotion, never before.** A staged row carries a JSON
  *proposal* — a different object from the association, as `HarmonisationProposal` is a
  different object from the value it proposes — so the table never holds rows pointing at
  questions that may never exist.

### The approval invariant, and every route around it

*An evidence-generated question cannot reach `APPROVED` with zero **verified** associations.*
A service guarantee, not a schema one: "at least one verified association" is a count query
over rows in other tables whose state changes independently.

Enforced in `question_service.update_question` — the choke point the UI, the copilot tool and
the CSV importer all arrive through. Two shortcuts reach `APPROVED` **without** going through
it, and both needed the check reached their own way: `harvest_service.promote(approve=True)`
(Run-to-Pipeline) and `_ensure_approved`. Both now refuse with the blockers named. Scoped to
`generation_method == "EVIDENCE"`, because applying it to the manual bank would block every
question that already exists without improving one of them.

What counts as verified is **per evidence family**, because each answers "is this checked?" in
its own vocabulary and collapsing them would hide which review happened: study/outcome/drug
fact `VERIFIED`, `NMAResult` *releasable*, candidate `ACCEPTED`, network `RATIFIED`.

That last one has a consequence worth stating: **a gap question needs a `RATIFIED` network.**
Its only association is the network, and an absence claim rests on the evidence set being a
faithful picture — ratification is exactly the review that says so. *"Nothing shows this"*
from a `DRAFT` network is an assertion about a set nobody has signed off as complete.

### What this means today

Prod verifies nothing, so on the prod corpus every pair attributes to `CURATION` and **no gap
question is generated at all** — correct, and the count tells a curator exactly how much work
stands between here and a question bank. Dev's 12 pilot-verified studies do produce 4
questions, none of which can be *approved* until their studies are curator-checked through
the surface built for it.

## Phase 8 — claim-level AI-vs-evidence evaluation — done

`app/evidence/claims.py` (pure: vocabulary, routing, 13 dimensions, grading),
`app/evidence/claim_extraction.py` (the one LLM call), `app/models/evaluation_claim.py`,
`app/services/claim_evaluation_service.py`, `app/api/claim_evaluation.py` (5 routes),
`tests/test_claim_evaluation.py` (46 tests). All 8 claim types, 8 classifications and 13
dimensions from the plan.

### The model observes; Python judges

The LLM extracts *structure* — subject, comparator, direction, magnitude, and which hedging
words were used — and is told explicitly that it is not evaluating anything. Every verdict
is then a pure function of stored data, so re-running the grader on the same rows next year
gives the same answer. A model asked to grade itself against evidence produces a finding
nobody can reproduce or appeal, which is not a thing to put in front of a medical reviewer.

### Routing is per claim, and the wrong authority raises

`EvidencePolicy` maps each claim type to the evidence families that can answer it. Grading a
boxed-warning claim against a league table raises `CategoryError` rather than returning a
low score — a category error has no correct verdict, and a confident wrong finding is worse
than no finding. `DIRECT_COMPARISON_CLAIM` and `RANKING_CLAIM` deliberately route
differently: a claim that two drugs were compared head-to-head is a claim about a *trial*,
and answering it from an indirect estimate would concede the point it asserts.

### Four ways to be confidently wrong, each closed

- **Absence read as contradiction.** A resolver gap makes an asserted winner `UNSUPPORTED`,
  never `CONTRADICTORY`. Telling a brand team *"the model contradicts our evidence"* when
  the truth is *"we have no evidence"* sends them to argue a case they cannot make.
- **An estimate read as a winner without knowing which way is up.** An ACR50 risk ratio of
  1.4 favours the treatment; an adverse-event risk ratio of 1.4 favours the comparator, and
  the arithmetic is identical. `canonical_outcomes.yaml` had no field for this, so
  **`benefit_direction` was added as required config with no default** and declared on all
  17 endpoints; an endpoint that omits it grades `NOT_COMPARABLE` rather than guessing.
- **Grading against an unapproved number.** The execution-mode table says `EXPLORATORY`
  output may not affect AI scoring, and an alignment dashboard *is* AI scoring. A
  non-releasable answer is reported as evidence unavailable.
- **A missing citation called a hallucination.** Our corpus holds only curated full-depth
  drugs, so "not in our store" overwhelmingly means "not ingested". Unresolvable citations
  are flagged `UNVERIFIABLE_CITATION` against citation quality. `HALLUCINATED_STUDIES`
  stays in the vocabulary but is unreachable without a positive registry check.

### Our own gaps never lower a model's score

`EVIDENCE_UNAVAILABLE` and `NOT_COMPARABLE` are excluded from `alignment_score` and reported
as `coverage` instead. Otherwise alignment would *fall as the evidence base thins* — exactly
backwards, and on today's prod corpus it would mark almost every response wrong. Coverage is
returned beside every score and should be read first: 1.0 on three checkable claims out of
forty is unmeasured, not aligned.

### Certainty calibration — and the defect the live run found

Symmetric by design. Over-claiming is the headline (definitive assertion, interval includes
no difference), but **under-claiming is also reported**: a model hedging where our evidence
is clean usually means the evidence is not reaching it, which is a Phase 9 communication gap.

The first live run against `NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16` graded *"there is
definitively no difference"* as `ALIGNED` while simultaneously calling it `OVERCLAIMED` — a
row no dashboard could render. The fix is the programme's own rule turned on itself: an
interval that includes no difference is an **absence of evidence, not evidence of
equivalence**, so a definitive equivalence claim over-claims exactly as much as an
unsupported winner. Calibration now takes `claims_a_winner` and the two agree.

The same run also printed `0.9592517401392111` where Phase 7's expected answer prints
`0.959`. Since Phase 8 grades against those expected answers, two formatters would make one
number look like two results — `question_generation.describe_estimate` is now the single
owner and both call it.

Live on the dev PsA network, Bimzelx vs Humira `RR 0.959 (95% CI 0.778 to 1.18)`:

| Claim | Classification | Certainty |
| --- | --- | --- |
| definitive superiority | `PARTIALLY_ALIGNED` | `OVERCLAIMED` |
| hedged superiority | `ALIGNED` | `CALIBRATED` |
| asserts equivalence | `PARTIALLY_ALIGNED` | `OVERCLAIMED` |
| no clear difference | `ALIGNED` | `CALIBRATED` |
| definitive inferiority | `PARTIALLY_ALIGNED` | `OVERCLAIMED` |

### Cost

One extra model call per response, on top of scoring. **Triggered, never automatic** —
`run_service` does not call it, so a scheduled full-bank run cannot double the bill without
someone asking, and a failed extraction can never fail a monitoring run.

### The dashboard — `/evidence/alignment`

`pages/EvidenceAlignment.tsx`, a sixth tab on the X2 sub-nav. **Coverage is the headline,
not the alignment score**, because the score deliberately excludes claims we could not check
and therefore rises as our corpus thins. The page refuses to show a score without coverage
beside it, and below 40% coverage it replaces the per-row score with `—` and says plainly
that the number describes too few claims to read. A safety contradiction pre-empts the
headline banner entirely.

Certainty calibration is rendered in both directions with its own explanation, so
under-claiming reads as *"our evidence is not reaching the model"* rather than as a model
fault.

### The trigger was unreachable from the UI

`api.evaluateRunClaims` existed in `client.ts` with **no call site**, so the empty state told
a brand marketer to `POST /claim-evaluation/runs/{run_id}` — an instruction the application
itself never followed. `EvaluateRunPanel` on the alignment page now owns it: a run picker
(runs are offered on `responses_success + responses_truncated`, exactly what the server
evaluates), the exact model-call count on the button before it is clicked, and the `limit`
passed through so the promised number is the number bought.

**The panel refuses to be a silent money hole.** Every grader routes to `VERIFIED` or
`RATIFIED` evidence, so on a store holding none of the three authorities a check returns
~0% coverage — true, and worth nothing. `GET /evidence/overview` already reports all three
counts, so the panel shows them as pills and, when all three are zero, blocks the button
behind an explicit acknowledgement that says what will be bought and what it will not
answer. Nothing was added to the backend for this.

## Phase 9 — synthesis + recommendations — done

The existing GEO engine was **extended, not rebuilt**, as the plan requires.
`app/remediation/implications.py` (pure), `app/remediation/evidence_gaps.py` (the second
finder), `app/services/evidence_synthesis_service.py`, plus new columns on `Recommendation`
and an evidence-specific prompt. `tests/test_evidence_recommendations.py` (26 tests).

### Two finders, one pipeline

`gaps.find_gaps` asks *how does the answer read?* — the brand scored `SECOND_LINE` or worse.
`evidence_gaps.find_evidence_gaps` asks *is the answer right?* — a specific claim our
evidence contradicts or cannot support. Both emit the same record shape, so
`engine._build_row` consumes either without knowing which produced it, and `source_type` on
the row says which did. A response can carry both, one, or neither.

### The refusal this phase exists for

Every positioning gap has a content remedy, so nothing in the old engine ever had to ask
whether one existed. Phase 8 findings often have none — and the worst of them looks exactly
like one that does:

> *"The model claims Rinvoq beats Drug X and our evidence cannot support that."*

If the comparison is genuinely unavailable, an honest *"no head-to-head data exists"* page is
the remedy. If it is unavailable because **nobody has verified studies we already hold**,
then proposing a comparison table sends a brand team to spend money while the actual fix is
an afternoon of curation. Phase 7's `attribute_gap` already separates those, so Phase 9 reads
it rather than re-deciding:

| Finding | Implication | Content? | Owner |
| --- | --- | --- | --- |
| contradicts the verified label | `AI_MISINFORMATION_RISK` | yes | Medical Affairs / Regulatory |
| hedges where our evidence is clean | `COMMUNICATION_GAP` | yes | Brand / Content |
| unsupported, gap unattributed | `MISSING_COMPARATIVE_DATA` | yes | Brand + Medical Affairs |
| unsupported, gap is **CURATION** | `INTERNAL_CURATION_REQUIRED` | **no** | Evidence curation |
| unsupported, gap is **PROTOCOL** | `INTERNAL_CURATION_REQUIRED` | **no** | Statistical review |
| unsupported, real evidence gap | `EVIDENCE_GENERATION_NEEDED` | **no** | Clinical Dev / HEOR |

`EVIDENCE_GENERATION_NEEDED` is excluded from content for a sharper reason than the curation
row: the remedy is a trial, and a content brief proposing to fill a genuine evidence gap with
a web page is how unsupported claims get written under our own name. The non-actionable
findings are **returned in the generate summary** rather than dropped — *"3 comparisons are
blocked by our own verification backlog"* is work someone owns, and a shorter recommendation
list would hide it.

### An aligned answer produces no work

`classify` returns `None` for an aligned, calibrated finding. A recommendation engine that
always finds something is not measuring anything.

### Confidence comes from governance, not from a model

The new `confidence` column is derived from the review state of the evidence behind the
finding: verified label and ratified network score higher than a single unreviewed
extraction, and a non-releasable result caps it at 0.4 — the same rule that stops an
exploratory number grading a response, one layer out. A recommendation is an instruction to
spend money, and one built on an extraction nobody has checked must not present itself as
certain.

### Safety outranks search volume

A contradicted boxed warning gets severity 3.0, above every other implication, so it cannot
be ranked below a second-line placement because a competitor happened to have more search
volume. The finder sorts by severity before applying `limit`, so it cannot fall off a page.

### Synthesis

`GET /evidence-questions/synthesis?indication=...` assembles the plan's readout — what the
evidence shows, what changed, evidence strength, limitations, competitor landscape, AI
alignment, strategic implications. **Assembled, never inferred**: every number is a stored
row some earlier phase decided, so there is no step here that can be wrong independently of
the phase that produced its input.

Limitations are a first-class section, not a footnote. Live on dev:

```text
WHAT THE EVIDENCE SHOWS: 2
  Bimzelx vs Humira: a risk ratio of 0.959 (95% CI 0.778 to 1.18) | crosses_null=True | L1
  Humira vs Taltz:   a risk ratio of 1.14  (95% CI 0.947 to 1.37) | crosses_null=True | L1
LIMITATIONS:
  - NETWORK_NOT_RATIFIED  The network is DRAFT. Nothing above has passed both reviews.
  - ROUTE_MIXING          The network mixes administration routes, a transitivity threat.
STRENGTH: verified 12 / 33 | network DRAFT
```

Both releasable comparisons cross no-effect. The honest readout for this indication today is
*"nothing we can release distinguishes these treatments, on a network nobody has ratified,
from a corpus that is 36% verified"* — and that is what the page says.

### The recommendations UI

`RecommendationsPanel.tsx` now distinguishes the two finders. An evidence row shows **what
the AI said** and **what our evidence shows** in place of the SEMrush block — search volume
is a fact about a positioning gap and says nothing useful about a claim that contradicts a
label, so the two are separate panels rather than one merged one. Confidence, certainty
verdict and gap attribution are surfaced as pills with the reasoning behind each in a
tooltip.

The important half is the **"N findings that content cannot fix"** panel, rendered *above*
the recommendation list. Those rows never reach a card at all — the engine does not generate
a recommendation for them — so without the panel a curation backlog would look like less work
rather than like work belonging to somebody else. Each entry names its owner and its next
step.

## The first live resolve on real registry data

Phase 6 was marked done against fixtures. This is the first time it ran on harvested
ClinicalTrials.gov data, and the engine held up: `GET /comparisons/matrix` returned 15 pairs,
**6 releasable, 0 gaps**, with real estimates and populated `considered` chains — e.g.
Bimzelx vs Humira `RR 0.959 (0.778-1.183)` from NCT03895203, flagged
`SINGLE_STUDY_NO_HETEROGENEITY_ESTIMABLE`. Asking for `GOVERNED` downgraded and recorded the
blocking status instead of raising.

**No pair hit `NMA_SERVICE_UNAVAILABLE`.** Bucher covered every indirect pair in-process,
exactly as the sidecar limitation above predicts. The undeployed sidecar has not yet cost
this programme a single comparison.

The run was worth far more for what it broke than for what it computed. Everything below was
invisible to the fixture suite.

### Arm defects the first live resolve exposed

Three landed fixes, all verified against re-parsed live payloads rather than fixtures:

- **664 orphaned outcome rows (10.5%) → 0.** The Phase 0 title-reconciliation fix assumed the
  two ID spaces were two *names* for one partition. They are two *partitions*: participant
  flow names a group by its whole journey (`"Placebo / Upadacitinib 15 mg"`), while an outcome
  measure names it as it stood at that timepoint (`"Placebo"`). Exact-title matching therefore
  failed for precisely the arms whose assignment changed later — **disproportionately placebo,
  the comparator every star network is anchored on**. In NCT03104400 it dropped placebo from
  all 18 measures. The union reading is arithmetic, not inference: that measure's `Placebo`
  group counts **423 = FG000 (211) + FG001 (212)**, so no existing arm can carry the row and
  `_measure_group_arms` mints one. Attaching it to either journey arm would have halved it.
- **`StudyArm.sample_size` was never populated — 116 arms, 0 with an N.** That left
  `row.sample_size or arm.sample_size` in the comparison service as a dead fallback. Now read
  from period 1's `STARTED` milestone, which is the randomised denominator; `COMPLETED` or a
  later period would silently shrink an arm to its responders. **This changed no current
  number** — every row in this corpus posts a denominator — so it is latent correctness only.
- **A `Total` results group would have become a network node.** It is not an enumerator, names
  no class, and `canonical_treatment` returns it unchanged as a plausible name, so **no
  existing predicate rejected it**. Pooled across studies on label identity that is a
  fabricated common comparator closing loops the evidence never contained — the "Standard
  Care" problem under a friendlier label. `treatments.is_aggregate_label` now rejects it.

The severity of the first one comes from the builder's reporting-arm reader (`_reporting_arms`,
`_treatments_of` before issue 3 renamed it), which skips a row whose `arm_id` is `None`
**silently**: the rows were absent from every network with no warning anywhere.

Two things worth not re-litigating:

- **An aggregate word alone is too blunt a signal.** My first pattern rejected `"Overall
  Survival Cohort Rinvoq"`, a real arm. The predicate now requires an opening aggregate word
  *and* no treatment anywhere in the label. My own test caught this, which is the argument for
  writing the false-positive case alongside the true one.
- **Minting same-treatment arms is safe for the graph.** `topology.build` collapses a study's
  treatments to a `frozenset`, so duplicate arms cannot create a self-loop.

### The builder and the resolver disagree about scope

`build_network` deliberately does not apply a protocol's time window — the comment at
`network_builder_service.py` says duplicating that judgement would put it in two places that
can disagree, and that is sound. The consequence was not anticipated: the builder reports
**8 nodes including Rinvoq, connected**, while any protocol-scoped resolve sees **6 nodes
without Rinvoq**.

That is the failure mode the "topology is not reimplemented" decision above exists to
prevent — *promising a comparison the resolver then refuses to compute* — reached through the
time window rather than through topology code. The decision is still right; the **reporting**
is what misleads, because a build report does not disclose that its topology is pre-protocol.

**Fixed by disclosure** — see issue 3 below. `BuildReport` now carries a `ProtocolScope`
next to its topology and the builder still applies no window to anything.

## X2 — the evidence store, read API + UI — done

`app/api/evidence.py` + `app/services/evidence_read_service.py`; 7 GET routes under `/evidence`,
18 tests in `tests/test_evidence_read.py`. The UI is now **9** pages behind **one** top-level
tab: `EvidenceOverview`, `EvidenceNetworks`, `EvidenceComparisons`, `EvidenceStudies`,
`EvidenceDrugFacts`, `EvidenceGovernance`, `CompetitorDiscovery`, `EvidenceAlignment`,
`EvidenceSynthesis`, with `EVIDENCE_SUBNAV` in `App.tsx` as the only place a later phase has to
touch. The top-level header is **10** tabs and scrolls; the point of the phase was to stop
that count growing per surface.

> **The sub-nav has reached the limit this phase existed to avoid.** It is now nine pills —
> the same width that makes the top-level header overflow. Counting it as "eight, which is
> close" was wrong twice over: wrong arithmetic, and a reassuring framing about a limit
> already reached. **The next evidence surface must fold into an existing tab as a second
> view or a filter, not become a tenth pill** — otherwise X2's crowding problem is
> reproduced one level down, which is precisely the outcome the consolidation was for.

That note has since been **overridden, once, on purpose** — it is left standing above rather
than edited, because a rule quietly rewritten to match what was built is not a rule.

> **AMENDMENT — the tenth pill was added anyway, deliberately.** `/evidence/ingest` is the
> tenth. The override has a reason and a mitigation, and both belong on the record:
>
> - **Why it did not fold.** Ingest is the only **write** surface in the section. Folding a
>   form that spends an external API budget and grows the corpus into a filter on a read page
>   would hide it exactly where nobody looks for it, and would put a write control one
>   accidental click from browsing — the same objection that keeps `/evidence` read-only in
>   the first place. A worse outcome than one more pill.
> - **The mitigation.** The sub-nav container is now `flex-wrap`, so ten pills reflow onto a
>   second line instead of reproducing the horizontal scroll the phase existed to fix. That
>   addresses the stated symptom rather than restating the limit.
> - **What has not changed.** The rule still holds for the eleventh. Wrapping buys one row,
>   not a licence.

**The gap it closes.** Phases 2-6 shipped three routers whose useful routes all take a
`network_id` — `/comparisons`, `/evidence-review`, `/published-syntheses` — and **nothing
exposed one**. Networks are assembled by `scripts/ingest_evidence.py`, so the entire governance
surface was reachable only by someone who already knew an id. Every phase was "done" and the
store was unbrowsable.

Three rules, each of them a refusal:

- **It writes nothing.** Verification, membership and ratification transitions stay on
  `/evidence-review`. A GET surface that could also decide would put a lifecycle transition one
  accidental click from a browse action.
- **It judges nothing.** Every status, flag and exclusion reason is reported as stored. The one
  derived figure is the protocol scope, and that is *asked of* the builder rather than
  recomputed here.
- **It hides no mismatch.** `mismatch_flags` travel with every outcome row, because a UI that
  shows a clean number for a row flagged `EVENTS_DERIVED_FROM_PERCENTAGE` is worse than no UI:
  it launders a caveat the extraction was careful to record.

`GET /evidence/networks/{id}` reports **both** topologies side by side, which is what made issue
3's disclosure reusable instead of CLI-only — see the `_screen` note under that issue. It is
derived per request and never cached, for the reason recorded there.

`GET /evidence/overview` leads with **canonical endpoint coverage**, not rows ingested. A row
with no canonical id is invisible to every network, so ingest volume is precisely the number
that flatters the corpus most: 6342 rows stored, 528 of them usable.

Two tests worth keeping: a `RATIFIED` network is still readable (`build_network` raises on one,
and a ratified network is the one people most need to look at), and a malformed `flags` column
degrades to `[]` rather than 500-ing the whole study page.

**What the UI refuses to do, matching the API.** No page posts to `/evidence-review` — a
verification or ratification decision is reached from the review surface that owns it, not from a
browse screen. Overview leads with canonical endpoint coverage and states the ingest count
*underneath* it, `EvidenceNetworks` renders the endpoint-level and protocol-scoped topologies
side by side rather than picking the flattering one, and an outcome row carrying
`mismatch_flags` shows them inline with the number. A reviewer who is not shown the caveat has
not reviewed the row.

## Evidence ingestion from the UI — `/evidence/ingest` — done

`app/api/evidence_ingestion.py` (5 routes under `/evidence-ingestion`), progress hooks on
`evidence_ingestion_service` + `network_builder_service`, `pages/EvidenceIngest.tsx` +
`components/IngestReport.tsx`, **22 tests in `tests/test_evidence_ingestion_api.py`** plus 2
service-level progress tests in `tests/test_evidence_ingestion.py`.

**The gap it closes.** All three corpus-growing routines existed only as CLI scripts, so
growing the evidence corpus required a shell inside the production container — which is why
the corpus has grown once. `POST /trials`, `POST /drug-facts` and `POST /reparse` are those
scripts, backgrounded, with `GET /status` for progress and the report returned as JSON.

**The report is the feature, not the button.** The CLI's value was never that it writes rows;
it was what it prints *before* it writes them. `IngestReport.tsx` renders the whole of it:
screened-out studies **with reasons** (rolled up by class above the per-study list), the three
label buckets each with the advice that applies to *that* bucket, extraction warnings, and both
topologies side by side with `nodes_lost_to_window` called out. A page that rendered a spinner
and a row count would have deleted the review step the dry run exists for.

Six refusals, each with a test:

- **Preview is the default and writes nothing — including no audit row.** A run that promises
  to write nothing must not write a row to say so, and an audit entry per preview would leave
  the log unable to answer *"what changed the corpus?"* without reading a mode field on every
  row. The report carries a `PREVIEW` badge and an explicit *"nothing was written"* line, so a
  preview cannot be misread as a completed ingest.
- **Nothing here can verify.** No request model accepts `verified_by` — they are
  `extra="forbid"`, so passing one is a **422 rather than an ignored key** — and no route
  reaches `verify_study`. `--verify-as` is deliberately not ported: bulk-stamping one name
  across studies nobody opened manufactures an audit trail that looks real. Verification stays
  one study at a time on `/evidence-review/studies/{id}/curator-check`.
- **A bad form fails at submit, not as a job that dies quietly.** 422 for an unknown
  indication, an outcome that is not that indication's (naming the known set, as the script's
  exit-2 does), an undefined protocol, or an unrecognised treatment phase.
- **One job at a time, across all three kinds** → 409. A re-parse racing an ingest fights over
  the same rows. The slot is claimed **in the request**, not in the background task: a
  `BackgroundTasks` callable runs after the response is sent, so a guard that only flipped the
  flag there would let two submissions a second apart both pass.
- **A `NetworkBuildError` on a `RATIFIED` network is prose in `error`, never a 500.** Refusing
  to rebuild the evidence set a reviewer approved is a correct decision; presenting it as a
  server fault would read as a bug.
- **The task owns its session** (`AsyncSessionLocal()`), because a request-scoped one is closed
  by the time the callable runs, and `commit=False` rolls back in a `finally`.

**Progress hooks are additive.** An optional in-place `progress` dict, the same contract
`harvest.pipeline.harvest` already uses, so every CLI caller passes nothing and behaves
identically — pinned by a test. `studies_total` is the **capped** list rather than `discovered`,
because a `limit: 3` smoke run reporting "3 of 37" looks stalled at the moment it finishes.

**One topology presentation, not two.** `TopologyPanel` moved out of `EvidenceNetworks.tsx`
into `components/TopologyPanel.tsx` and both surfaces import it. A second copy would eventually
disagree with the first about whether a graph is connected — the same argument that keeps
`evidence/topology.py` as the single implementation on the backend.

**The risks, stated.** No job persistence (in-memory, single process — the limitation
`/harvest/status` already has; a refresh loses the report and more than one uvicorn worker
would show per-worker state). **No RBAC**: `evidence_ingestion_api_enabled` is the *only* gate,
so anyone who reaches the UI can spend the external API budget and write to the corpus. And
preview-then-commit is two harvests of a live source, so a commit can legitimately differ from
the preview that was read — the toggle says so.

## Recording a decision from the UI — `/evidence/governance` — done

Four methods on `api/client.ts`, a rewritten `pages/EvidenceGovernance.tsx`, and
`scripts/verify_all_evidence.py`. **No new routes and no new tests** — see *What is not covered*.

**The gap it closes.** X1 shipped the review surface and X2 rendered it, but of the four gates
between an ingested study and a `GOVERNED` number, only two had a button. Study verification had
`CurationPanel` and drug labels had `EvidenceDrugFacts`; **protocol approval and network
ratification had backends, tests, and nothing that could reach them.** The effect was not an
error anywhere — `governance_gate` correctly returned `PROTOCOL_PENDING_APPROVAL` forever, so
every resolve ran `EXPLORATORY` and the UI rendered exactly as designed. A gate that nothing can
open is indistinguishable from a gate nobody has opened yet.

**One reviewer name, three panels.** `MembershipPanel` had its own name field; adding two more
would have invited three spellings of the same person across three lifecycles, which is a worse
audit trail than one field visibly serving all of them. It is deliberately not persisted — a name
that survives a refresh eventually signs for someone else's judgement.

**The original caution was kept, not overridden.** The old comment on `EvidenceGovernance.tsx`
said recording a decision was omitted on purpose, because "putting that button next to a browse
action is how a sign-off happens by accident". That objection was right about *placement* and
wrong about *existence*, so the controls sit in a dashed-border **Record a decision** strip below
each protocol rather than inline with the status chips. Reachable is not the same as easy.

**Consequences are shown before the decision, not discovered after it.** Three cases:

- **Ratifying freezes membership** — `decide_membership` refuses on a `RATIFIED` network, so the
  statistical stage carries that warning *before* the button, next to a pointer at the membership
  panel below.
- **Rejections and revocations prompt for their mandatory reason** client-side. The server
  enforces it either way; asking first means a refusal is not how the reviewer learns the rule.
- **Only an `APPROVED` role offers Revoke.** Offering it on a pending role would produce the
  service's *"no active decision to revoke"* — a button whose only outcome is a refusal.

**The stage buttons are not a second state machine.** `RATIFICATION_STEPS` maps each action to
the one status it applies from, which is the shape of the route set itself — `/submit` only ever
means `DRAFT -> PENDING_MEDICAL_REVIEW`. The server still enforces ordering; the map only avoids
rendering a button whose sole possible outcome is a 400. The gate verdict is shown *beside* the
buttons because protocol approval and ratification are **independent** gates rather than a
sequence: a ratified network under an unapproved protocol is still not governable, and reading
that off two separate panels is how someone ratifies, sees nothing change, and reports a bug.

**`scripts/verify_all_evidence.py` is the bulk half, and refuses to be the judgement half.** It
walks both curator queues, because `gather_evidence` skips an unverified study *even in
`EXPLORATORY` mode* — 21 of the dev tree's 33 studies were still `EXTRACTED`. It does not touch
protocol approval or ratification: those are decisions about method and evidence-set fitness, not
loops. Two properties worth keeping:

- **`--as` defaults to the dev pilot marker, imported from `reparse_dev_pilot` rather than
  restated.** A second copy of that string would let the two drift apart silently, and the guard
  it feeds is the only reason the re-parse hatch still exists — `VERIFIED` has no outbound edge.
  A real name closes it permanently, so the script says so loudly before it writes.
- **A dry run runs the real reproducibility check and rolls it back.** Both services accept
  `commit=False`, so unlike `ingest_evidence --verify-as` this is not a simulation of what would
  happen. It ends by printing `governance_gate` per network, so a run that verified everything
  still states that two gates remain.

**What is not covered.** No new tests, and the reason is that no new backend behaviour exists:
all four routes and both services were already covered by `test_evidence_review*.py`, and the
change is client wiring plus a dev script over them. That is an honest gap in a different sense —
**there is no frontend test infrastructure in this tree at all**, so the panels are verified only
by `tsc -b` and by hand. The first frontend regression will not be caught by CI.

**Still true after this.** No RBAC, so every name on this page is recorded and not authenticated —
the banner says so and now sits above real buttons rather than a read-only table. And approving
`PSA_ACR50_W16_PRIMARY` locks in the disputed weeks 14-18 window, so the first `GOVERNED` resolve
drops SELECT-PsA 1 and answers a Rinvoq question without Rinvoq. That is issue 1, unchanged; the
approval is revocable and re-derives as `SUPERSEDED` if the window is later widened.

## Phase 5 — competitor discovery, Tier A — code and tests done, never swept live

`app/evidence/discovery.py` (pure, no DB import), `app/services/competitor_discovery_service.py`,
`app/models/competitor_candidate.py`, `app/api/competitor_discovery.py` (7 routes under
`/competitor-discovery`).

**Tier A ships only what is mechanically derivable** from evidence already ingested. Six
reasons, each a fact about stored rows rather than an opinion about the market:

| Reason | Derived from |
| --- | --- |
| `DIRECTLY_COMPARED_TREATMENT` | randomised alongside a treatment we monitor |
| `PUBLISHED_NMA_TREATMENT` | a node of an ingested published synthesis |
| `APPROVED_INDICATION_COMPETITOR` | its own label names this indication |
| `SHARED_COMPARATOR_TREATMENT` | network topology — a comparator we also use |
| `PIPELINE_INDICATION_COMPETITOR` | registry development phase, no posted results |
| `NEWLY_ACTIVE_TRIAL_TREATMENT` | registry start/posting date inside the window |

Decisions worth not re-litigating:

- **Nothing here writes `brands.yaml`.** Accepting a candidate records a decision and makes it
  eligible for `GET /competitor-discovery/config-proposal`, which renders a YAML fragment for a
  human to commit. The whole argument for a curated class/route table is that it is a reviewable
  artefact; a queue that edited the file directly would convert it into the inferred kind, which
  is explicitly out of scope.
- **A decided candidate is never overwritten** — the same rule ingestion keeps for a decided
  study. A re-sweep refreshes signals on `NEW`/`DEFERRED` and leaves `ACCEPTED`/`REJECTED` alone,
  so a rejection is remembered rather than re-proposed every run.
- **Tier B2 stays out.** The module never assigns a `drug_class` curation did not hand it.
  Labelling something "same-class" by guesswork is worse than no label in a system with a
  medical review gate.
- **Placebo is not a competitor**, and neither is `Total`, `Standard Care`, `Arm B` or `bDMARD`.
  `is_discoverable` delegates every one of those judgements to `evidence.treatments` instead of
  restating them — a second opinion about what an arm label means is how a fabricated node
  reaches a curated config file.

**45 tests now cover it** (`tests/test_competitor_discovery.py`), and the false-positive cases are
the ones that earned their place: an already-curated drug is never proposed, `Placebo`/`Total`/
`Arm B`-style labels never become candidates, and a `REJECTED` candidate survives a re-sweep
rather than being re-proposed. Two of those tests had to be rewritten against the live taxonomy
after the first drafts asserted on drugs curation already owns — the fixture was wrong, not the
service.

**Still not proven on real data.** The sweep has never been run against the harvested corpus, so
the precision of the six reasons is a claim about logic, not a measurement. Look at a dry run
before the button, because **the UI's "Run sweep" commits** — populating the queue is what the
sweep is *for*, and the write it makes is confined to `competitor_candidate`:

```http
POST /competitor-discovery/sweep?commit=false   # reports what it would store, writes nothing
POST /competitor-discovery/sweep                # persists; decided candidates untouched
```

The case that will find a defect first is a real arm label that reads like an aggregate — the
precedent is the predicate above, which rejected `"Overall Survival Cohort Rinvoq"` until a test
caught it.

## Open issues, in priority order

Written to be picked up independently. Each names the files it touches so parallel sessions
do not collide. **Closed entries are kept, not deleted** — the reasoning is what stops the same
defect being reintroduced, and issue 5 in particular records two premises that turned out to be
wrong.

| | Status | Owner |
| --- | --- | --- |
| 1 — headline comparison does not resolve | **open** | statistical reviewer (proposal written) |
| 2 — outcome rows carry no canonical endpoint | done | — (what else to model is a reviewer scope call) |
| 3 — build report overstates what is answerable | done | — |
| 4 — 12 VERIFIED studies hold a stale parse | done, applied | — |
| 5 — data-quality items | done | — |
| 6 — resolver chooses silently between duplicate rows | done | — |
| 7 — an add-on arm resolves to the Placebo node | done | — |
| 8 — two dose arms of one study collapse to one node | **open**, new | statistical reviewer, then engineer |
| 9 — a frozen network had no exit; supersession still has none | **partly done**, new | engineer |

Issues 6 and 7 came out of issue 5 and are the two things it deliberately did not fix, because
both live outside `sources/clinicaltrials.py`. Issue 8 came out of issue 6 the same way: fixing
the per-arm choice made the neighbouring per-*treatment* one visible. Issue 9 came out of a
user clicking `INCLUDED` on a ratified network and being told to do something impossible.

### Issue 1 — the headline comparison does not resolve (protocol vs evidence)

`Rinvoq vs Humira` returns `NETWORK_DISCONNECTED`: *"Rinvoq does not appear in any scoped
trial."* The chain is exact and **contains no code defect**:

- `PSA_ACR50_W16` allows weeks 12-24 (`canonical_outcomes.yaml`)
- `PSA_ACR50_W16_PRIMARY` narrows to weeks 14-18 — a *legal* narrowing, so validation passes
- SELECT-PsA 1 (NCT03104400) reports ACR50 at **week 12**, which is the real trial design;
  its primary endpoint was ACR20 at week 12
- week 12 < 14, so every Rinvoq row is rejected `DIRECT_EVIDENCE_UNSUITABLE`, and Rinvoq
  drops out of the scoped topology entirely
- only **6 of 33** studies carry a week-16 ACR50 row, which is exactly the
  `included_study_count: 6` in the scoping report

**The approved protocol structurally excludes the trial the programme exists to analyse.**
Widening the window to admit week 12 is defensible and common in published PsA NMAs, but it is
a **methodology decision for the statistical reviewer**, it is approval-gated, and editing the
YAML retires existing approvals by design. Do not "fix" this by widening a window to make data
fit — that is precisely what the window check exists to stop.

Owner: statistical reviewer, not an engineer. Files: `analysis_protocols.yaml` (proposal only).

**Proposal written up for the reviewer in `docs/EVIDENCE_NMA_ISSUE1_PSA_WINDOW_PROPOSAL.md`**
— four candidate windows with consequences, the unapplied YAML diffs, and the approval
mechanics. It recommends nothing, by design. Two things it surfaced that are not in the
summary above:

- **A wide window makes the contributed timepoint nondeterministic.** `gather_evidence` keys
  usable arm data by treatment and assigns unconditionally, so two in-window rows for the same
  arm means **the last one silently wins**, decided by row order rather than by the protocol
  and flagged nowhere. `[14, 18]` is narrow enough that this cannot arise today; every
  widening option is exposed to it. This constrains the choice of bound more than the study
  count does.
- **The protocol currently requires an analysis its own window forbids.** Its sensitivity rule
  says *"Restrict to the direct Rinvoq-versus-Humira evidence"* — the evidence week 14-18
  excludes. Leaving the window unchanged means rewording that rule.

A related **engineer-owned** item, tracked separately so it is not mistaken for a methodology
fix: `_gap` checks node absence *before* `unsuitable_direct`, so this case reports
`NETWORK_DISCONNECTED` / "Rinvoq does not appear in any scoped trial" when the truth is that
the trial exists and the protocol excluded it. The correct reason is already in `considered` at
Level 1, so nothing is lost, but the headline inverts the finding. Same disclosure failure as
issue 3, reached through the resolver instead of the builder.

### Issue 2 — 91% of outcome rows carry no canonical endpoint — **done**

**The framing above was wrong, and is worth recording as wrong.** A 91% ambiguity rate *was* a
property of the corpus; the matcher's vocabulary needed nothing. `scripts/endpoint_mapping_audit.py`
groups every unmapped row by the matcher's own reason code, and the 91% was **three unrelated
things wearing one flag**:

| recomputed reason | rows | measures |
| --- | --- | --- |
| `NO_CANONICAL_WORDING` — names no endpoint we model | 5713 (90.1%) | 472 |
| `MATCHED_WORDING_AND_TIMEPOINT` | 310 (4.9%) | 50 |
| `TIMEPOINT_OUTSIDE_ALL_WINDOWS` — endpoint known, week rejected | 283 (4.5%) | 25 |
| `AMBIGUOUS_WORDING_AND_TIMEPOINT` — **the real ambiguity** | 36 (0.6%) | 1 |

Real ambiguity was **36 rows and one measure**, not 5785. The flag was chosen by
`len(match.candidates) > 1`, and the no-wording branch returned `tuple(scoped)` — *every*
endpoint modelled for the indication. PsA has three, so three "candidates" > 1, so every
unmatched row reported as ambiguous. `candidates` now means candidates and travels **empty**
when nothing was recognised; the vocabulary moved to `scoped`; each failure names itself in
`reason_code` so no caller infers one from a tuple length again.

#### The defect the 91% was hiding: a visit series is not one timepoint

`timeFrame: "Weeks 2, 4, 8, 12, 16, 20 and 24"` parsed to **2** — the first number, though the
docstring promised the largest — while the measure's `classes` are one per visit, titled
`"Week 2" … "Week 24"` (verified against NCT03158285's retained payload: 7 classes × 3 groups =
21 rows). Every visit inherited one measure-level week, and it cost rows in **both** directions:

- **Discarded**: no PsA window admits week 2, so a trial's week-12, -16, -20 and -24 ACR20
  values were dropped while sitting in the database.
- **Wrongly mapped, which is worse**: `"at Weeks 24, 28, 36, 44 and 52"` parsed to 24, *inside*
  the 12-24 window, so all **30** rows were stamped `PSA_ACR20_W16` — including the values
  measured at weeks 28 through 52. An unmapped row is a gap a curator can see; a wrongly mapped
  one is a wrong number in a network.

`parse_timepoint_weeks` now separates a **range** from a **list**. `"Weeks 12 to 24"` is one
assessment through week 24 and still collapses to its upper bound, but a list of visits returns
`None`, because picking one of seven — first, largest, or otherwise — assigns real trial values
to a week the source never claimed. `and` is no longer a range separator for the same reason.
Each class then resolves its **own** week and keeps `TIMEPOINT_FROM_VISIT_CLASS`, so a repeated
measure is never silently read as the trial's pre-specified analysis at that week.

**Measured across all 33 retained payloads, offline** (`TIMEPOINT_FROM_VISIT_CLASS` on **5306 of
6342 rows** — 84% of the corpus was resolving its timepoint from the wrong level):

| | before | after |
| --- | --- | --- |
| rows with a canonical endpoint | 310 | 296 |
| `ENDPOINT_AMBIGUOUS` | 5785 | **0** |
| `ENDPOINT_NOT_CANONICAL` | 247 | 6034 |
| `TIMEPOINT_NOT_PARSED` | 5370 | 64 |
| `STRATIFIED_RESULT` | 5528 | 148 |

Mapping did **not** go up, and that is the point: the fix removes out-of-window visits that had
been inheriting an in-window week, and they outnumber the week-12/16 visits it recovers. The
36-row ambiguity is now genuinely **resolved** rather than relabelled — that measure's classes
name the members (`ACR 20` / `ACR 50` / `ACR 70`) while its title named all three, so issue 5's
`_class_endpoints` identifies each class from its own title and the week-12 ACR20 class alone
carries `PSA_ACR20_W16`. Per-visit weeks and per-class identity only work together: either
alone leaves the other's rows mis-filed.

`reparse_stored_payloads` reports 452 rather than 296 because 12 of 33 studies are `VERIFIED`
and keep their pre-fix rows — including NCT03158285 and NCT03162796, the two studies whose ACR20
series diagnosed this. Freeing them is issue 4's `scripts/reparse_dev_pilot.py`, deliberately a
separate audited act.

**What is left is not a matcher problem.** 6034 rows name no modelled endpoint: `"Percent Change
From Baseline in ACR Components"`, `"SF-36 Norm Based Scores"`, PK, adverse events, and the
`PASI 75` / `PASI 100` / `ACR 70` classes the registry posts constantly. PsA models exactly three
endpoints, so no tokenisation can match those. Whether to model more is a **scope question for
`canonical_outcomes.yaml`** and a methodology decision, not a tuning exercise — which is why
that file was not touched here.

**Coupling to watch:** `clinicaltrials._class_endpoints` tests `EndpointMatch.candidates` to
decide whether a class names an endpoint **at all**. Changing what `candidates` carries changes
which rows keep a canonical id, so that field is load-bearing in two files now.

Files: `app/evidence/endpoints.py`, `app/evidence/sources/clinicaltrials.py`,
`scripts/endpoint_mapping_audit.py`, `tests/test_evidence_clinicaltrials.py`.

### Issue 3 — a build report overstates what is answerable — **done**

`BuildReport` now reports **two** topologies. The endpoint-level one is unchanged — still
assembled, still stored, still what the memberships are proposed against — and a
`ProtocolScope` sits beside it: the same proposed evidence re-read through the governing
protocol's approved window, carrying `nodes_lost` (`('Rinvoq',)` on the live PsA case),
`studies_out_of_window`, and its own topology summary. `report.overstates_answerable` is the
one-line answer, and it is logged as a warning on any build where it is true.

Three things worth not re-litigating:

- **The window check is asked, never repeated.** `_protocol_scope` calls
  `protocols.in_approved_window`, the same predicate extraction screening and the resolver's
  scoping use. No week arithmetic entered the builder, so there is still exactly one
  implementation of what a week means.
- **Nothing is filtered.** An out-of-window study stays a `PROPOSED` member and the stored
  topology keeps its node. A window is one protocol's judgement and can be re-approved
  without re-harvesting, so enforcing it here would be the second copy that eventually
  disagrees — precisely the defect this disclosure exists to reveal.
- **The scoped topology is not persisted.** It would be a second stored truth that goes
  stale the moment a window is re-approved. The resolver derives its own scope every run;
  this is disclosure for the report, the CLI and the audit entry only.

With no governing protocol the scope is `None` rather than an empty graph — with no approved
window there is nothing to narrow, and an empty scoped topology would claim nothing is
answerable. A rebuild that names no protocol scopes the disclosure to `network.protocol_id`,
because that is what the resolver will read.

`scripts/ingest_evidence.py` prints both blocks, the first labelled *pre-protocol*, and names
the lost nodes under `NODES LOST TO WINDOW`. Verified: `tests/test_evidence_ingestion.py`
35 passed (5 new), full suite 976 at the time.

**Reused by X2, not duplicated.** The eligibility screen was then extracted out of
`build_network` into `_screen`, so `protocol_scope_for(db, network)` can answer the same
question on a GET without going through a function that mutates the session, refuses a
`RATIFIED` network and writes an audit entry. Two copies of the screen would drift, and a read
surface that screened studies differently from the builder would report a scope for a network
nobody assembled.

Files: `app/services/network_builder_service.py`, `scripts/ingest_evidence.py`,
`tests/test_evidence_ingestion.py`.

### Issue 4 — 12 VERIFIED studies hold a stale parse — **decided and applied**

The arm fixes could not reach them. `ingest_study` returns `SKIPPED` for a `VERIFIED` or
`REJECTED` study, and `lifecycles` gives `VERIFIED` no outbound edge, so there was no legal
transition back. The 12 included NCT03104400. They were verified as
`DEV PILOT - extractions not reviewed` to make anything resolve at all — a **dev-database
artefact, not a curation record**.

**Decision: reset out-of-band in dev, and re-parse from the retained payloads rather than
from a fresh harvest.** Measuring the corpus first is what settled it. The split was not
between good rows and stale ones — *no* study in the database carried the fixed parse, because
the arm fixes had only ever been verified against re-parsed payloads in memory. The 21
`EXTRACTED` studies were merely reachable. And the 12 held 4813 of 6342 outcome rows and were
**100% of the only network's membership**, so accepting the split meant every number the
resolver produced stayed pre-fix.

Nothing was protected by refusing. All 12 carried the marker that says nobody reviewed them,
stamped across six seconds by the `--verify-as` loop; all memberships were `PROPOSED`, the
network `DRAFT`, `nma_results` empty, no protocol approved. The lifecycle rule was guarding a
machine's assertion that it had checked nothing.

**Re-parse, not re-harvest**, because a stale parse is a defect in our code and not in the
source. All 66 payloads are retained in full (`PUBLIC_DOMAIN` / `FULL_INDEFINITE`), so the
extraction was redone against the exact bytes the original attested to. Re-harvesting would
have moved the parser *and* the source data in one step, and any estimate that then shifted
would be unattributable.

Verified against the dev SQLite file:

| | before | after |
| --- | ---: | ---: |
| orphaned outcome rows | 664 | **0** |
| arms | 116 | 132 |
| arms carrying a randomised N | 0 | 54 |
| rows with a canonical endpoint | 310 | 528 |
| retained payloads | 66 | **66** |

The payload count holding at 66 is the provenance check: `_identical_payload` matched on
checksum and reused every row, so re-parsing minted no second document for the same fetch.
The reset is on the record as `DEV_PILOT_VERIFICATION_RESET` in `audit_log` — the one thing
the lifecycle forbids is at least not invisible.

**The production answer is still the `version` / `superseded_by` chain** that
`evidence_ingestion_service` declares out of scope. It is the only way to correct a decided
row without rewriting it, and it must exist before a real curator verifies anything. Today
`--verify-as` is the only route to `VERIFIED` and it hands out a state with no exit; that hole
is unchanged.

**The first run wrote to the database while reporting a dry run** (`ed4c8ef`). `verify_study`
committed unconditionally, so the caller's later `rollback()` rolled back an empty transaction
and everything queued before it — the verification reset *and* the re-parse — was already
durable. An unconditional commit inside a service is not a local convenience: it silently voids
every caller's ability to not write, and the damage is proportional to how much the caller had
queued. `test_verify_study_honours_commit_false` pins it by rolling back an ingest that ran
before the verify.

**Still outstanding: the network was never rebuilt.** Every study re-parsed at 23:09:40-42
while `evidence_networks.updated_at` still reads 21:09:12, so the stored `treatment_nodes`,
`comparator_edges` and `is_connected` were computed from the old arms. Any resolve run now
reads a topology that predates the fix. Rebuild it **offline** — `ingest_evidence` re-harvests,
which reintroduces the two-variables problem this whole approach exists to avoid:

```bash
python -m scripts.reparse_stored_payloads --indication "Psoriatic Arthritis" \
    --rebuild-network PSA_ACR50_W16 --protocol PSA_ACR50_W16_PRIMARY --commit
```

**No longer blocked.** The `identities` double-bind in `_parse_outcomes` — the per-class
endpoint list from `_class_endpoints` and the `(arm_id, outcome_id, week)` accumulator sharing
one name, raising `AttributeError: 'list' object has no attribute 'setdefault'` on every parse
— is fixed: the list is `class_identities` and the accumulator is `identity_measures`.

Run the rebuild **after** reading issue 5 below, not before. The same parse now also drops 12
later-period arms and withholds 76 canonical ids that were attached to the wrong endpoint, so
a network rebuilt now is a different network from the one the table above describes.

Still true on the latest read of the dev file: the network reports `updated_at` 21:09:12 with
all 8 nodes and `is_connected` set, while the studies report 23:09:42. The stored corpus is
also still the **issue-4** parse rather than issue 5's — 6342 rows, **528** canonical, 132 arms
in the file today — so the rebuild has to follow a re-parse, not stand in for one.

**The command above will not do it on its own.** `reparse_stored_payloads` reports a `VERIFIED`
study as SKIPPED, which is correct and is the entire reason this issue existed — and the pilot
run re-verified all 12 as it finished (`verified_at` 23:09:42, against the pilot marker). So it
re-parses 21 of 33 studies and would rebuild the network from a corpus where 12 still hold the
pre-fix parse. That is also why **452 is not what the current parser produces**: it is 21
studies re-parsed plus 12 left untouched. All 33 re-parsed offline gives **296** canonical rows,
the figure issue 2 records. The order is `reparse_dev_pilot --commit`, then the rebuild.

Files: `app/services/evidence_ingestion_service.py` (`reparse_study` / `reparse_studies`),
`scripts/reparse_dev_pilot.py`, `scripts/reparse_stored_payloads.py`,
`tests/test_evidence_ingestion.py`.

### Issue 5 — data-quality items — done

All four investigated in `sources/clinicaltrials.py`. Two were the defects they looked like.
The other two were **framed wrongly**, and the fourth was hiding the most serious extraction
error in the programme so far.

- **`'Placebo / Upadacitinib 15 mg'` carrying `dose_value=15.0` — fixed.** `_parse_dose` read
  the first strength anywhere in the label. Ten arms did this across **four** separators —
  `/`, `to`, `Followed by`, `Plus` — so splitting on punctuation does not generalise.
  `_arm_dose` attributes by **exclusivity** instead: a dose is recorded only when the label
  describes one agent at one strength. Placebo has no strength of its own; two distinct
  strengths mean no single `dose_value` is true of the arm; a second named agent means the
  strength cannot be assigned to either. `label` and `dose_description` keep the full title, so
  what is withheld is only the claim that it is *this node's* dose.
- **10 participant-flow arms for a 4-arm trial — fixed.** `_later_period_groups` excludes a
  group that a later period counts and period 1 does not, which is the same reading
  `_arm_sample_sizes` already uses to refuse period 2's N. It requires **positive evidence**: a
  record with one period, or no counts in period 1, keeps every group. Safe because
  `_measure_group_arms` mints a group back on demand if a measure names it. NCT03104400 is 10 →
  6; the residual 6-against-4 is the deliberate placebo-partition minting, not period
  duplication.
- **`EVENTS_DERIVED_FROM_PERCENTAGE` on 2468 rows — the flag was honest, nothing counted it.**
  `ParsedStudy.flag_counts` now carries the census with the parse. Investigating it found a
  real defect underneath: **185 of 597 posted measures post class-level `denoms` and the parser
  ignored them.** In NCT01695239 the measure states 38 participants for a group while its
  `PASI 75` class states 7 for the same group, and *neither* reading survives arithmetic — only
  1 of the 185 is consistent with the class figure being a numerator, and dividing 10.9% by 7
  yields one participant. The N is therefore unknown, so no count is derived when the two
  disagree. That is not conservatism: a count divided by a denominator the source itself
  disputes is not a lossy number, it is a wrong one.
- **`STRATIFIED_RESULT` on 5528 rows — the premise was false.** Only **16** of 327 multi-class
  measures are endpoint families, **311** are visit series, and **zero** measures in the corpus
  are multi-category. So "most rows are subgroup rows" was wrong, and the population filter was
  not doing unverified work — it was doing **none**: this parser never sets
  `population_stratum`, so every row reads unstated and matches any network scoped unstated.
  `STRATIFIED_RESULT` is now 148 rows rather than 5528.

#### What the stratified flag was hiding

**65% of canonical, arm-attached rows (159 of 245) were groups where one arm held many
different numbers for one endpoint at one week**, and `gather_evidence` keys
`usable[arm.treatment]`, so whichever row it read last became that arm's number.

The cause: the canonical id and the week were read off the **measure** title while the class
axis carried the member. `NCT02319759:OM041` is titled "…Achieved a PASI 50, PASI 75, PASI 90
and PASI 100 Response" over "Weeks 24, 28, 32, 44, and 56" and posts 20 classes. Because only
PASI 90 is modelled for PsA, the measure title matched it **unambiguously** — so all 20 rows,
PASI 50, 75 and 100 among them, were stored as `PSA_PASI90_W16` at week 24. `match_endpoint`'s
refusal to guess was never defeated; it was bypassed by asking about the family instead of the
member.

`_class_endpoints` fixes it in three parts, none of which guesses:

1. When **any** class names canonical wording, every class is identified from its **own** title.
2. A sibling naming a different member does not inherit the family's id
   (`ENDPOINT_NOT_NAMED_BY_CLASS`). `candidates` is the test rather than `matched`, so a class
   whose wording is recognised but whose week is out of window still counts as having named it.
3. Two classes left claiming one id at one week are **both** withheld
   (`ENDPOINT_NOT_DISTINGUISHED_BY_CLASS`). Their numbers differ, so at most one can be that
   endpoint's value, and picking either is the guess this module exists not to make. The row
   keeps the registry's wording and its class title, so curation loses nothing.

#### Verified offline against the 33 retained payloads

Re-parsed from stored bytes, never re-harvested, for the reason in issue 4.

| | stored | re-parsed |
| --- | ---: | ---: |
| placebo arms carrying a dose | 10 | **0** |
| arms | 132 | 120 |
| NCT03104400 arms (a four-arm trial) | 10 | 6 |
| colliding groups **within** a measure | 44 | **0** |
| `STRATIFIED_RESULT` rows | 5528 | 148 |
| `EVENTS_DERIVED_FROM_PERCENTAGE` rows | 2468 | 1650 |
| `DENOMINATOR_DISPUTED_BY_CLASS` rows | — | 2665 |
| rows with a canonical endpoint | 528 | 452 |

The `ENDPOINT_AMBIGUOUS` → `ENDPOINT_NOT_CANONICAL` reclassification and the per-visit
timepoints come from **issue 2's** in-flight work in the same file, not from these fixes. The
452 is a whole-file count taken with the pilot's 12 `VERIFIED` studies skipped, so it
*understates* the change — all 33 re-parsed gives 296.

NCT03895203 still keeps Placebo (n=281), Bimzelx (n=431, 160 mg) and Humira (n=140, 40 mg) with
three canonical rows each, so the headline releasable pair survives. `pytest -q` = **1033
passed** at the time, 15 of them new.

#### Two things deliberately not fixed here

Both are outside this file, so they are written up as **issues 6 and 7** below rather than left
in a subsection nobody scanning the issue list would find: the resolver choosing silently
between duplicate postings, and `canonical_treatment` resolving an add-on arm to Placebo. The
parse discloses each and acts on neither.

`population_stratum` also remains unset by this parser. It cannot be filled from the registry's
class titles: they say `BSA >= 3%` while `canonical_outcomes.yaml` strata are prior-therapy
(`BIO_NAIVE`, `TNF_IR`, …), and writing free text into a column networks are split on is worse
than leaving it unstated. The case that mattered — two strata claiming one endpoint at one week
— is caught by the collision guard instead.

Files: `app/evidence/sources/clinicaltrials.py`, `tests/test_evidence_clinicaltrials.py`.

### Issue 6 — the resolver chooses silently between duplicate rows — **fixed**

`gather_evidence` assigned `usable[arm.treatment] = payload` unconditionally, so when two in-scope
rows described one arm **the last one read won** — decided by row order, and flagged nowhere.

Issue 1's proposal reached this from one direction (a widened window admitting two timepoints).
Issue 5 reached it from the other, and the second route needs no widening at all: the registry
itself posts one result twice, once as its own measure and once inside a combined by-visit
measure. Two independent paths to one defect is the argument for fixing it where the choice is
made rather than upstream of either.

Measured on the 33-payload corpus **after** issue 5 removed every within-measure duplicate:

- **44** endpoint/week/arm identities are reported by more than one measure
- **18** carry identical numbers — latent, since no result moves whichever row wins
- **26** differ, and those are where the silent choice changes a number
- at least one pair sits at **week 16** (`PSA_ACR20_W16`, NCT02319759), so this is not only a
  consequence of widening a window; it depends on which endpoint a protocol scopes

The parser cannot settle it. The rows are two faithful readings of two analysis populations, and
discarding either would throw away correct evidence — 18 of the 44 are the same number twice. It
now emits a per-study warning counting them. The fix is a rule at the point of selection: refuse
and report, or choose under something the protocol actually states.

**It refuses.** Rows are now collected per `arm_id` and reduced to one value each, in `arm_id`
order so nothing depends on how the database returned them. Agreement collapses — identical
duplicates are one fact stated twice, and withholding those would discard correct evidence to
solve a problem that case does not have. Disagreement withholds the arm and reports
`AMBIGUOUS_ARM_DATA` naming every candidate value, e.g. *"arm Rinvoq has 2 contradictory
in-scope results for PSA_ACR50_W16 (week 14: 45/100; week 16: 51/100)"*.

It did **not** choose, because nothing states a preference. The endpoint definition carries a
`nominal_timepoint_week` and preferring the row nearest it would be defensible, but it is a
methodology rule and there is no protocol field for it — inventing one in a resolver is how an
engineer's judgement ends up inside an approved analysis. If the statistical reviewer wants that
rule, it belongs in `analysis_protocols.yaml` beside `dose_policy`.

Why a status of its own rather than `INSUFFICIENT_ARM_DATA`: *missing* and *contradictory* send a
reviewer to different places. The first is nothing to act on; the second names a study whose
analysis populations somebody must choose between. That is the same argument
`PUBLISHED_SYNTHESIS_UNSUITABLE` was added for.

Six tests. The one that matters most is not the refusal but **order-independence**: the same
conflict inserted in the opposite order must produce a byte-identical finding, which is also why
the message sorts its candidates before formatting. A report that varied with row order would
have reintroduced the defect in the reporting.

Files: `app/services/comparison_service.py` (`gather_evidence`, `_select_arm_payload`),
`app/evidence/resolver.py` (`EvidenceSet.ambiguous_arms`), `app/evidence/statuses.py`,
`app/api/comparisons.py`, `tests/test_comparison_service.py`.

### Issue 7 — an add-on arm resolves to the Placebo node — **fixed**

`canonical_treatment` checked `_PLACEBO_RE` **before** the drug catalog, so a label naming placebo
became the placebo node no matter what else it named.
`'Group 2: Guselkumab 100 mg q4w Plus Placebo'` — a Tremfya arm with add-on placebo, from
NCT05071664 — resolved to `Placebo`.

That is worse than one mislabelled arm. Placebo is the anchor every indirect comparison chains
through, so an arm that received an active drug is pooled into the common comparator and
Bucher's transitivity assumption gets applied to a node that is partly Tremfya. It surfaced only
because issue 5's dose rule warns when an arm resolves to placebo while stating a strength;
nothing else in the pipeline noticed.

**The exclusivity shape proposed above would have been wrong, and is worth recording as wrong.**
*Placebo wins only when no curated agent is named* breaks two label shapes that are genuinely
placebo arms:

- **`'Placebo Plus MTX'`** — methotrexate is curated *because* `brands.yaml` calls it "the
  background and active-control arm in most PsA and RA trials", so exclusivity would hand the
  node to Methotrexate and empty the placebo node across most of the corpus. That is the same
  harm as the bug, in the opposite direction: not a mislabelled node but a **missing common
  comparator**, which is the failure this module's own header warns takes Bucher's anchor with it.
- **`'Placebo / Upadacitinib 15 mg'`** — names a curated agent, so exclusivity would move a
  crossover arm that received no active drug during the primary window into the Rinvoq node. Six
  existing parametrized cases assert it stays placebo, so exclusivity would have failed the suite.

**Order is the discriminator instead: registries name the randomised allocation first.** Placebo
wins only when the placebo token precedes every curated agent in the label. That resolves all
three shapes without preferring either component by category:

| label | node | why |
| --- | --- | --- |
| `'Placebo / Upadacitinib 15 mg'` | `Placebo` | placebo through the primary window, then a crossover |
| `'Placebo Plus MTX'` | `Placebo` | placebo against a background every arm shares |
| `'Guselkumab 100 mg q4w Plus Placebo'` | `Tremfya` | guselkumab from randomisation, placebo is the double dummy |
| `'Upadacitinib 15 mg / Placebo'` | `Rinvoq` | withdrawal design — active drug first |

`agents_in`'s span-claiming loop was extracted to `_agent_positions`, which now returns offsets
rather than discarding them, so both callers read one implementation. `is_placebo` delegates to
the same predicate, because the coverage audit reads the flag stored on the arm while
`is_aggregate_label` and `_reference_of` call the function — two answers to "is this a placebo
arm" is how the builder and the audit would come to disagree about one study.

Nothing here decides whether an add-on arm deserves a node of its own. That is the same kind of
question as dose pooling and belongs to `dose_policy` under an approved protocol; this only
decides which node the arm is not silently pooled into.

Verified: `pytest -q` = **1090 passed**, 12 of them new, including the two false-positive guards
above and a drift check pinning `is_placebo` to `canonical_treatment`'s second return. The
add-on arm now also records the 100 mg that issue 5 was correctly withholding from it, since the
dose is attributable once the node is Tremfya.

**What this does not fix.** The parser changed; the stored corpus did not. Every `study_arms` row
keeps its old `is_placebo` and `treatment` until a re-parse, so the network rebuild in issue 4
must follow this fix rather than precede it. And the Phase 0 route measurement is **downstream of
exactly this call** — `_collect_placebo_rates` reads `StudyArm.is_placebo` for both the route
attribution and the rate collection, so an add-on arm's drug response was being banked as a
placebo response for that trial's route. `PHASE0_COVERAGE.md`'s oral-versus-SC spreads, and the
direction reversal between UC and CD currently attributed to small n, are unre-measured and may
have a mechanical explanation. Re-measure offline from the 33 retained payloads, not by
re-harvesting, for issue 4's reason.

Files: `app/evidence/treatments.py`, `tests/test_evidence_clinicaltrials.py`. The `_reference_of`
note above was misfiled — it lives in `app/services/comparison_service.py`, and it re-resolves
node names rather than raw labels, so it is unaffected either way.

### Issue 8 — two dose arms of one study collapse to one node

Found while fixing issue 6, and **not fixed with it**. Issue 6 was one *arm* described twice;
this is two *arms* resolving to one node. Parsed from the committed SELECT-PsA 1 fixture:

```text
'Placebo' <- 'Placebo'                    dose None
'Rinvoq'  <- 'Upadacitinib 15 mg QD'      dose 15.0 mg
'Rinvoq'  <- 'Upadacitinib 30 mg QD'      dose 30.0 mg
'Humira'  <- 'Adalimumab 40 mg EOW'       dose 40.0 mg
```

Dose is stripped from node names **by design** — `treatments.py` says so, and keeps `dose_value`
structured precisely so `dose_policy` can decide later. But `dose_policy` is a *required*
protocol field, every protocol sets it to `SEPARATE_BY_APPROVED_DOSE`, and **no service reads
it**: `protocols.py` validates the enum and that is the end of it. `topology.build` even
documents the collapse as "a `dose_policy` decision made before this point" — and no point before
it makes one. So one dose arm's numbers silently stand in for the treatment, contradicting the
approved protocol, and silent dose pooling is the criticism these modules warn about most often.

**Disclosed, not resolved.** `gather_evidence` now reports `arms_sharing_a_node` naming both
labels, and the collapse is at least deterministic (`arm_id` order) rather than DB order. One
test pins that disclosure so the finding cannot quietly disappear. It is not fixed here because
every repair is a decision above an engineer:

- **Separate** the doses and every node in every stored network is renamed — the builder,
  `topology`, published-NMA matching and the curated catalog all key on the bare name.
- **Pool** them and that contradicts the `SEPARATE_BY_APPROVED_DOSE` on the approved protocol.
- **Pick one** and somebody has decided which dose the programme's headline number is about.

Which one is the reviewer's call, and it is approval-gated either way. It also interacts with
issue 1: the widened-window proposal and this both change what SELECT-PsA 1 contributes.

Files: `app/evidence/treatments.py`, `app/evidence/topology.py`,
`app/services/network_builder_service.py`, `app/services/comparison_service.py`,
`app/config/analysis_protocols.yaml`.

### Issue 8 — a withdrawal trial's co-arms read as head-to-head evidence — **fixed**

Found by the **first live competitor-discovery sweep**, run on prod's 37-study PsA corpus. The
sweep proposed `CAN` as a competitor, "Randomised head-to-head against a treatment we monitor",
`compared_with` Cosentyx, Olumiant, Orencia, Rinvoq, Taltz and Xeljanz — six head-to-head
comparisons from **one** study.

NCT05080218 randomises *interrupt* against *continue* on whichever biologic each patient was
already taking. Its nine arms:

```text
'Treatment Interruption - UPA'      -> Rinvoq                 class_node=False
'Treatment Interruption - ABA'      -> Orencia                class_node=False
'Treatment Interruption - TOF'      -> Xeljanz                class_node=False
'Treatment Interruption - SEC'      -> Cosentyx               class_node=False
'Treatment Interruption - BAR'      -> Olumiant               class_node=False
'Treatment Interruption - IXE'      -> Taltz                  class_node=False
'Treatment Interruption - TNFi SQ'  -> TNFi                   class_node=True
'Treatment Interruption - CAN'      -> CAN                    class_node=False
'Treatment Continuation'            -> Treatment Continuation class_node=False
```

**Eight of nine arms resolve to clean molecule names**, so `is_discoverable` passes every one of
them — the per-treatment guard structurally cannot see this. The trial ran no comparison between
those drugs at all; the only arm that gives the study away is the seventh. `CAN` reached the
queue because it is an uncurated abbreviation the drug catalog does not resolve, which is exactly
the signal the queue exists to raise — but on a comparison that never happened.

The network builder was never at risk: `_screen` already excludes the whole study on
`is_class_level_node`, which is why `CAN` is absent from
`NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16`. **The screen existed in one of the two consumers.**
Discovery selected on `indication` + `is_randomised` and nothing else.

Fixed with `discovery.is_strategy_trial(arm_treatments)` — study-level, delegating to the same
`treatments.is_class_level_node` the builder uses, so the two cannot drift apart. The sweep now
reports `strategy_trials_screened` rather than dropping studies silently. The regression test
fails without it with the exact live symptom: `assert 'CAN' not in {'CAN', 'Sotyktu'}`.

**Verified in prod after deploy (`d674670`).** The sweep now returns three candidates —
Methotrexate, Sonelokimab, ABT-122 — and `strategy_trials_screened: 4`:

```text
NCT03739853  ['Combination csDMARD', 'Early TNF Inhibition', 'Standard Care']
NCT05080218  ['CAN', 'Cosentyx', 'Olumiant', 'Orencia', 'Rinvoq', 'TNFi', 'Taltz',
              'Treatment Continuation', 'Xeljanz']
NCT07138898  ['Shorter hold', 'Standard hold']
NCT07149792  ['Prescreen Based bDMARD Stategic', 'Standard bDMARD']
```

**Four, not the three the re-parse appeared to warn about.** `reparse_stored_payloads` printed
`(16 studies)` and then listed `flagged[:15]`, so the fourth was silently off the end — fixed
here by printing the remainder, because a count above a shorter list reads as the whole list.
NCT07149792 is the trial `_CLASS_LEVEL_TERMS` already names in a comment as the case the DMARD
rule catches without anyone enumerating the `Stategic` typo; it holds no molecule at all.

One consequence to look at: `treatments_observed` fell 31 -> 19 and `already_tracked` 12 -> 10, so
**two monitored brands have no molecule-level PsA trial evidence outside a withdrawal trial.**
That is the honest number rather than a regression — a withdrawal trial never was evidence about
those drugs against each other — but it is a coverage fact worth a reviewer's attention.

**Two things this leaves open, deliberately:**

- **`'Treatment Continuation'` resolves to a node of that name with `class_node=False`.** A
  withdrawal trial with no class-level arm would still pass the screen and contribute it as a
  molecule. `_CLASS_LEVEL_TERMS` has no term for continuation/interruption/withdrawal, and adding
  one changes the **builder's** network composition too, so it is a separate change with a wider
  blast radius — not a rider on this one.
- **`CAN` is probably a real molecule** (an abbreviation, like every sibling arm), just one the
  catalog cannot resolve. Screening the study is correct regardless: the *reason* attached to it
  was false. If that drug should be tracked, it belongs in `drug_catalog` by curation, not by a
  fabricated head-to-head.
- **The stale row survives in prod.** The pre-fix sweep persisted four candidates, and a sweep
  only creates or updates — it never deletes a candidate it no longer finds. So
  `CC-PSORIATIC-ARTHRITIS-CAN` stays `NEW` in the prod queue after the fix deploys. Rejecting it
  is the wrong record (a rejection asserts someone judged the *molecule* unsuitable, when what
  was wrong was the proposal), and there is no delete route. Needs a decision.

Files: `app/evidence/discovery.py`, `app/services/competitor_discovery_service.py`,
`tests/test_competitor_discovery.py`.

### Issue 9 — a frozen network had no exit, and supersession still has none

Found by a user clicking `INCLUDED` on `NET-PSORIATIC-ARTHRITIS-PSA_ACR50_W16` and being
refused. The refusal was correct. **The remedy it named did not exist.**

Three callers refused to touch a frozen network and all three said *"Supersede it and build a
new version instead"*: `decide_membership`, `build_network` and `scripts/reparse_dev_pilot.py`.
There was no supersede route, no service function, no button, and nothing anywhere wrote
`SUPERSEDED` to a network. `_network_out` made it worse by publishing `allowed_transitions`
straight off the state machine, so the API advertised `SUPERSEDED` and `DRAFT` as available
from `RATIFIED` while implementing neither. A ratified network was a dead end, and on prod it
had been ratified with **zero membership decisions and nothing verified** — nothing in
`record_statistical_review` requires either, so the frozen "approved evidence set" was all 15
proposed studies, none individually screened.

**Two defects fixed, one deliberately deferred.**

**Fixed — the exit.** `reopen_network` + `POST /evidence-review/networks/{id}/reopen` take any
reopenable state back to `DRAFT`. Reason mandatory (it withdraws a review that happened), person
recorded, `NETWORK_REOPENED` audited. The review stamps are cleared from the row, because a
`DRAFT` still displaying a `statistical_reviewer` reads as approved to anyone scanning; the audit
context carries the cleared names and dates, so the row states what is true now and the log
states what was true. It applies to both pending stages and `REJECTED` as well as `RATIFIED` —
all four already had a legal edge to `DRAFT`, so the panel's *"REJECTED is terminal, supersede
and rebuild"* copy had been wrong about the machine it was describing.

**Fixed — one owner for "do not touch".** `build_network` and `decide_membership` refused only on
`RATIFIED`, so both would happily rewrite the graph or the membership under a reviewer who was
part-way through reading it. `reparse_dev_pilot.py` had the broader rule right and kept it in a
private tuple. `lifecycles.FROZEN_FOR_EDIT` + `is_frozen_for_edit()` + `frozen_explanation()` are
now the one owner and all three call it — decision 14, applied to a rule that had three opinions
and two of them wrong. `frozen_explanation` exists so a mid-review refusal does not tell a
reviewer they already approved something. `SUPERSEDED` is deliberately **not** in the frozen set:
a retired network is nobody's live evidence set, and there is a test pinning that it stays
rebuildable.

**Deferred — real supersession, and why it is not a rider on this.** Reopening keeps one row and
**no snapshot of what was approved**. Retaining the approved set as its own row while a
replacement is built is what `EvidenceNetwork.version` / `superseded_by` are for, and
`evidence_synthesis_service._network_for` and `remediation/evidence_gaps.py` already filter
`superseded_by IS NULL` in anticipation. The blocker is identity: `network_id_for()` is
deterministic on (indication, outcome, phase, stratum) and `build_network` looks the row up by
`network_id` alone. So a naive `supersede` that only set the status would be **worse than
nothing** — the next rebuild finds the same row, the `RATIFIED` guard no longer fires, and it
overwrites the nodes and edges of the snapshot it was meant to preserve. Doing it properly means
deciding whether identity is the id or the 4-tuple, and cascading a rename across
`NetworkMembership`'s FK. `DrugFact` already has the worked two-row pattern
(`evidence_ingestion_service` ~977) to copy from. Until then `reopen` is the honest action for an
approval that should not have happened, and the wrong one for a set you may need to show later —
both the route docstring and the UI say so.

Files: `app/evidence/lifecycles.py`, `app/services/evidence_review_service.py`,
`app/services/network_builder_service.py`, `app/api/evidence_review.py`,
`scripts/reparse_dev_pilot.py`, `frontend/src/api/client.ts`,
`frontend/src/pages/EvidenceGovernance.tsx`, `tests/test_evidence_governance.py`,
`tests/test_network_membership.py`, `tests/test_evidence_ingestion.py`,
`tests/test_evidence_ingestion_api.py`.

**One test changed rather than added**, worth flagging because it looks like a weakened
assertion and is not: `test_a_ratified_network_surfaces_the_refusal_as_error_prose` pinned
`"Supersede it" in status["error"]`. Its intent is that the refusal reaches the operator as prose
carrying a remedy — it was passing while the remedy was unreachable. It now pins
`"Reopen it to DRAFT"`.

## Study curation — the gate that actually blocks output

**Three gates, not one, and they are routinely conflated.** Prod fails all three, but only
one of them stops a number existing at all:

| Gate | Asserts | Who | Prod |
| --- | --- | --- | --- |
| **Study verification** (Lifecycle 1) | this extraction matches its source | a **curator** | 37 at `EXTRACTED` |
| Network ratification (Lifecycle 3) | this evidence set is fit to compute on | clinician + statistician | `DRAFT` |
| Protocol approval | this methodology is sound | clinician + statistician | none |

The last two only decide `GOVERNED` vs `EXPLORATORY`. **The first stops everything**, because
`gather_evidence` takes `require_verified: bool = True` and applies it in EXPLORATORY mode too
— *"computing on unverified extractions would produce a number whose inputs nobody has
checked, which is not exploratory, it is wrong"*. So an unverified corpus yields no result,
approved protocol or otherwise.

And verification is a **data-accuracy** step, not a clinical one — `verify_study` says it
asserts *"a person checked the extraction against the source"*, and its audit entry is
written as `CURATOR` while the other two are `REVIEWER`. That distinction is what makes the
blocking gate clearable today, without waiting on a physician.

**What was missing was anywhere to do it.** There was no HTTP route to verify a study — only
`verify_study()` in Python and the scripts' `--verify-as`, which bulk-stamps one name across
every study in scope. Using it on prod would record a named human as having checked 37
extractions they never opened, which is worse than leaving them unverified because it
manufactures an audit trail that looks real.

`app/services/study_curation_service.py` + three routes on `/evidence-review/studies`:

- **`GET /studies?network_id=…`** — the queue, scoped to whatever a resolve would consult.
  An unknown id is a 404, not an empty list.
- **`GET /studies/{id}/source-check`** — re-derives the study from its retained payload and
  diffs it against the stored rows. **Read-only**, deliberately not `reparse_study`, which
  delegates to `ingest_study` and therefore deletes and rewrites what it re-derives. Same
  `ctg.parse`, so there is still one parser opinion.
- **`POST /studies/{id}/curator-check`** — the confirmation, refused while a difference is
  outstanding.

**A clean diff proves reproducibility, not correctness.** A parser that misreads a
denominator misreads it identically twice and the diff stays silent. The response therefore
carries the source URL and `flag_counts` — `EVENTS_DERIVED_FROM_PERCENTAGE` covered 2468 of
6342 rows in one PsA harvest — because *those* are what send a curator to the registry record.
The UI states this in the green case rather than presenting a tick as an answer.

**Why the refusal matters:** `ingest_study` SKIPS a `VERIFIED` row. Verifying a stale
extraction freezes it beyond the reach of the ordinary re-parse, leaving an out-of-band reset
as the only remedy. Being told to re-parse first is the cheaper failure.

### An empty INCLUDED set is not an empty corpus

The first live call returned `"total":0` with *"no INCLUDED members, so no study verification
would change what it can resolve"* — **which was false, and was my bug.**

`network_builder_service` creates every membership as **`PROPOSED`** and *nothing in the
system promotes one to `INCLUDED`* — there is no route, no service call and no script. So
every network in every environment has an empty `INCLUDED` set. `gather_evidence` survives
this by accident of truthiness:

```python
if included and study.study_id not in included:   # empty set is falsy -> filter skipped
```

So the resolver reads an empty set as *"membership narrows nothing"* and falls back to every
study in the network's indication. The curation queue read the same empty set as *"nothing
qualifies"* and reported the opposite conclusion. **Verification really is the binding gate**
— the original advice was right — but the queue said it was pointless.

Fixed by extracting `comparison_service.membership_filter()`, which returns `None` rather
than an empty set precisely so the two callers cannot disagree about which case it is, and
by having the queue delegate to it. `gather_evidence` now reads `if included is not None`,
which is behaviour-preserving. A test asserts both sides read the same rule.

**Still open:** promoting `PROPOSED` -> `INCLUDED` has no implementation. Today that is
harmless because the resolver falls back to the indication, but it means *the network's own
membership decisions are not yet expressible* — a study screened out of a network by a human
cannot be recorded as such, and `EXCLUDED` requires a reason that nothing collects.

### "37 blocking" was true and useless

The corrected queue returned all 37 prod PsA studies — and **20 of them carry
`canonical_outcome_count: 0`**. A study with no canonical row cannot change what the network
resolves however carefully it is verified, so the honest queue length was never 37.

The corpus is also carrying studies that are not adult PsA efficacy trials at all:

| Study | What it actually is | Canonical rows |
| --- | --- | --- |
| NCT02714322 | Mylan adalimumab biosimilar in **plaque psoriasis** | 0 |
| NCT04018599, NCT06291948 | Phase 1 PK in **healthy subjects** | 0 |
| NCT04261010, NCT06729463, NCT07128472 | gene-expression / SNP association studies | 0 |
| NCT06100744 | risankizumab in **children** (juvenile PsA) | 0 |
| **NCT04527380** | ixekizumab in **children** (juvenile PsA / ERA) | **2** |

The first six are inert. **NCT04527380 is not** — it is a paediatric trial holding canonical
rows, so verification is the only thing keeping it out of an adult network. Worth a look too:
**NCT02814175 CONTROL** (a treatment-*strategy* trial, 6 rows) and **NCT05071664 AFFINITY**
(guselkumab + golimumab **combination** arms, 4 rows).

So the queue now asks `comparison_service.outcome_in_scope` — the resolver's own rule, made
public rather than reimplemented — for each study, and reports `could_contribute`,
`in_scope_arm_count` and `withheld_*` per row plus a `worth_verifying` total. Contributors
sort first. A contrast needs in-scope data on two arms, so one arm counts as non-contributing.

### The live answer: 9 — and Rinvoq is not one of them

`worth_verifying: 9` of 37. **`NCT03104400` SELECT-PsA 1 is `could_contribute: false`**, and
so is `NCT02349451` ABT-122. Those are precisely the two the topology loses under the
protocol window (issue 1), now visible per study instead of as a node count. The nine that
can contribute are OPAL BROADEN, the guselkumab Phase 2a, the risankizumab PoC, CONTROL,
NCT03158285, Discover-1, BE OPTIMAL, NCT04527380 and SOLSTICE.

**A network that resolves without Rinvoq does not answer the question anyone is asking.**
Verifying all nine would produce a real EXPLORATORY number comparing competitors to each
other, with the focus drug absent. That is worth knowing before anybody spends a day curating.

Two of the nine also need a second look before they are treated as usable: **NCT04527380** is
paediatric (juvenile PsA / ERA) and **CONTROL** is a treatment-strategy trial.

### The reason list was hiding the reason

First cut sorted `out_of_scope_reasons` alphabetically and truncated to three. Every study
reports dozens of endpoints, so *"measures HAQ-DI, not PSA_ACR50_W16"* filled all three slots
— and sorts ahead of *"reports week 12, outside the approved window"*. **The output showed
only noise and systematically hid issue 1**, which is the one refusal a human must act on.
Same failure as the truncated-warning fix in `d6b6e8f`: truncating without ranking.

Now only refusals of rows that **measure this network's outcome** are reported, as
`withheld_row_count` + `withheld_reasons`. A study with `withheld_row_count > 0` and
`could_contribute: false` is a **protocol casualty** — no amount of curation fixes it — and
those study ids are collected into a top-level **`protocol_blocked`**, so a reviewer's
decision is not filed as curator backlog.

Files: `app/services/study_curation_service.py`, `app/services/comparison_service.py`,
`app/api/evidence_review.py`, `tests/test_study_curation.py` (16 tests),
`frontend/src/pages/EvidenceStudies.tsx`, `frontend/src/api/client.ts`.

## Gotchas

- `brands.yaml`, `canonical_outcomes.yaml` and `analysis_protocols.yaml` loaders are all
  `lru_cache`'d — **restart the backend** after editing. In tests, patch `_config` and call
  `protocols.protocols.cache_clear()` on both sides of the assertion.
- No `conftest.py`; each test file defines its own in-memory `session` fixture.
- PowerShell mangles `\n` inside `python -c "..."` — use semicolons or a here-string.
- **Dev and production are different databases.** `bitbucket-pipelines.yml` excludes `data`
  from the rsync and `ec2_deploy.sh` mounts the SQLite file from the host, so a data fix
  applied locally does *not* reach the box. Any backfill must be run twice —
  `docker exec evidence-monitoring-agent python -m scripts.<name>` on EC2. Production held
  far more unlabelled rows than dev (53 questions and 1823 responses vs none), so this is
  not a theoretical distinction.

### `therapeutic_area` on historical responses is deliberately left stale

The disease backfill fills `response.disease` but never rewrites `response.therapeutic_area`.
For the 8 questions corrected `Immunology → Rheumatology`, their past responses therefore
read `therapeutic_area=Immunology` alongside `disease=Rheumatoid Arthritis`, which looks
self-contradictory and is not.

Those runs genuinely scored against Immunology competitors — that was the bug. Relabelling
them `Rheumatology` would assert an evaluation context that never existed. **So for
historical analysis `disease` is the trustworthy dimension; `therapeutic_area` on an old
response records the context the run actually used.** Grouping old responses by
`therapeutic_area` will undercount Rheumatology.
