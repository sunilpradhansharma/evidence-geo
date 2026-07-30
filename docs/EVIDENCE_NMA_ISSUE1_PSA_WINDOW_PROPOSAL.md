# Issue 1 — PsA ACR50 approved time window: proposal for the statistical reviewer

**Decision required from:** the named statistical reviewer, with the medical reviewer, since
both roles must sign the resulting content hash.
**Owner:** not an engineer. This document is a **proposal only** and changes nothing.
**Protocol:** `PSA_ACR50_W16_PRIMARY` in `backend/app/config/analysis_protocols.yaml`.
**Blocks:** the programme's headline comparison, `Rinvoq vs Humira` in Psoriatic Arthritis.

`GET /comparisons/resolve` for `Rinvoq vs Humira` returns `NETWORK_DISCONNECTED`. There is no
defect in the window logic. The approved protocol structurally excludes the one trial that
randomised the two drugs against each other, so the question the programme exists to answer
is currently unanswerable by construction.

Widening the window is defensible and common in published PsA network meta-analyses. It is
also a methodology decision, it is approval-gated, and it must not be made to fit the data.
That is exactly what the window check exists to prevent.

## The chain, verified

Every step below was read in the working tree, not inferred.

| Step | Where | Value |
| --- | --- | --- |
| The endpoint permits weeks 12-24 | `canonical_outcomes.yaml:113` | `allowed_window: { min_week: 12, max_week: 24 }`, `nominal_timepoint_week: 16` |
| The protocol narrows to weeks 14-18 | `analysis_protocols.yaml:120` | `approved_time_window: { min_week: 14, max_week: 18 }` |
| Narrowing is legal, so startup validation passes | `evidence/protocols.py:305-310` | errors only when `a_lo < o_lo or a_hi > o_hi` |
| A week-12 row is refused with its reason | `services/comparison_service.py:106-110` | `"reports week 12, outside the approved window [14, 18]"` |
| SELECT-PsA 1 (NCT03104400) reports ACR50 at week 12 | first live resolve, `EVIDENCE_NMA_PROGRESS.md` | the real trial design; its primary endpoint was ACR20 at week 12 |
| Rinvoq therefore leaves the scoped topology entirely | — | 6 of 33 studies carry a week-16 ACR50 row, matching `included_study_count: 6` |

Nothing here is a bug. Each component does what it was specified to do, and the refusal is
recorded rather than silent: the rejection reason is carried in the answer's `considered`
chain at Level 1.

## The window is the only lever

An extraction-level workaround does not exist, by design. A `HarmonisationProposal` that
proposed reading week 12 as week 16 is **auto-rejected with no escalation path** whenever it
falls outside the approved window (`evidence/extraction.py:197-227`). Routing it to a human
would invite someone to overrule an approved statistical protocol from a review queue.

So the only legitimate way to admit SELECT-PsA 1 is for the statistical reviewer to change
`approved_time_window`, and to own that change.

## The hazard that constrains every widening option

**Read this before choosing a bound.** In the scoping loop, usable arm data is keyed by
treatment and assigned unconditionally:

```@c:\Users\SushantBandgar\Desktop\evidence-monitoring-agent\backend\app\services\comparison_service.py:218-232
        for row in study.outcomes:
            if row.arm_id is None or row.arm_id not in by_arm:
                continue
            in_scope, reason = _outcome_in_scope(row, network, window)
            if not in_scope:
                if reason:
                    rejections.add(reason)
                continue
            arm = by_arm[row.arm_id]
            payload, payload_reason = _arm_payload(arm, row, expected_type=outcome_type)
            if payload is None:
                if payload_reason:
                    rejections.add(payload_reason)
                continue
            usable[arm.treatment] = payload
```

If a trial reports the same endpoint for the same arm at **two timepoints that both fall
inside the window**, the second row silently overwrites the first. Which timepoint the trial
contributes is then decided by row iteration order rather than by the protocol, and nothing
is flagged.

Today this cannot arise: `[14, 18]` is narrow enough that at most one reported timepoint
falls inside it. Any widening changes that. The consequence for this decision:

- A window admitting **exactly one** reported timepoint per trial is deterministic.
- A window admitting **two or more** makes the contributing timepoint arbitrary and
  undisclosed, which is worse than either timepoint chosen deliberately.

Whether a given candidate window admits two timepoints per trial in the current corpus is
**not measured**. That measurement is the per-window sensitivity report, deferred.

> **AMENDMENT (2026-07-27) — the hazard above has been FIXED, and the measurement has been
> taken.** The section is left standing rather than deleted, because it is the argument the
> options below were written against and a reviewer needs to see what changed under them.
>
> **1. A widened window no longer picks a timepoint by row order.** The unconditional
> `usable[arm.treatment] = payload` quoted above is gone. Rows are now collected per arm and
> reduced by `_select_arm_payload`, which collapses duplicates that agree and **refuses** when
> they disagree — naming every candidate value and recording it in `scoping.ambiguous_arms`:
>
> ```@c:\Users\SushantBandgar\Desktop\evidence-monitoring-agent\backend\app\services\comparison_service.py:189-196
>     **Agreement collapses, disagreement is refused.** Duplicates carrying identical numbers
>     are one fact stated twice, so withholding them would discard correct evidence to solve a
>     problem that case does not have. Rows that differ are two faithful readings of two
>     analysis populations, and choosing between them is a curation judgement about which
>     population the analysis is of — not something a resolver may settle, and *emphatically*
>     not something the order rows come back from the database may settle.
> ```
>
> Its docstring names *"a widened window admits two timepoints at once"* as a case it handles.
> So the consequence of a two-timepoint window is no longer *an arbitrary undisclosed number*
> — it is *a withheld arm with a stated reason*. *"Worse than either timepoint chosen
> deliberately"* no longer describes the outcome, and **the strongest argument against C and D
> no longer holds**.
>
> **2. The per-window counts are measured.** Taken from the EC2 corpus on 2026-07-27, 90
> studies `VERIFIED`, via `scoping.skipped` on a `/comparisons/resolve` call. Every trial
> holding a **mapped** `PSA_ACR50_W16` row that only the window rejected names its timepoint
> there:
>
> | Rejected at | Trials |
> | --- | --- |
> | week 12 | `NCT02349451`, `NCT03104400` (SELECT-PsA 1 — Rinvoq) |
> | week 24 | `NCT01695239`, `NCT03151551`, `NCT03671148`, `NCT03675308`, `NCT05071664` |
>
> | Option | Window | Studies in scope | Rinvoq in? |
> | --- | --- | --- | --- |
> | A | `[14, 18]` | 9 (measured) | no |
> | B | `[12, 16]` | 11 | **yes** |
> | C | `[12, 18]` | 11 | **yes** |
> | D | `[12, 24]` | 16 | **yes** |
>
> **B and C are indistinguishable on this corpus.** No trial reports a mapped ACR50 row at
> week 17 or 18, so lowering the floor to 12 is the only thing either edit does. That is one
> fewer decision to make, not a reason to prefer either.
>
> `NCT02745080` and `NCT04882098` are excluded on **endpoint mapping**, not the window — no
> widening admits them. They are Issue 2, not Issue 1.
>
> **These are upper bounds.** A row admitted by a wider window still has to survive
> `_arm_payload`: two arms need a posted event count and denominator before the study
> contributes a contrast. 11 and 16 are the ceilings on `included_study_count`, not promises.
>
> **3. What is still unmeasured, and now cheap.** The seven trials above each name exactly
> **one** rejected timepoint, so none of them individually brings two in-window rows under D.
> But a study that already contributes is never recorded in `scoping.skipped` — `gather_evidence`
> only lists a study there when it yields fewer than two usable arms — so whether any of the
> **current 9** also reports ACR50 at week 24 is not visible from outside. That is the residual
> Option D exposure. It is now one call to measure, because the fix in (1) makes it self-report:
> set the candidate window, restart, resolve any pair, and read `scoping.ambiguous_arms`. Zero
> means the window is deterministic on this corpus.

## The options

No option is recommended here. Consequences only. **Read the amendment above first** — it
supersedes the hazard argument these four options were originally weighed against, and attaches
a measured study count to each.

### A. Leave `[14, 18]` unchanged

- Rinvoq stays out of the PsA network. The headline comparison remains unanswerable.
- 6 studies remain in scope and the estimand stays literally true. (6 was the dev corpus. The
  same window measures **9** on the EC2 corpus of 2026-07-27 — see the amendment. The figure
  moves with the corpus; the exclusion does not.)
- Defensible if the reviewer's position is that a week-12 ACR50 reading cannot stand in for a
  week-16 estimand. That is a real statistical position, not an evasion.
- **Cost to disclose:** the protocol's own sensitivity rule becomes unsatisfiable. It
  instructs, at `analysis_protocols.yaml:138`, *"Restrict to the direct Rinvoq-versus-Humira
  evidence and compare with the network estimate"* — evidence its own window excludes. If A is
  chosen, that rule should be removed or reworded in the same edit, otherwise the approved
  protocol requires an analysis it forbids.

### B. `[12, 16]`

- Admits weeks 12 and 16, excludes 18-24. Rinvoq re-enters via SELECT-PsA 1.
- Matches the window already used for every PsO, RA and AD endpoint in
  `canonical_outcomes.yaml`, so it is the house convention rather than a bespoke bound.
- Requires the estimand to be restated: a pool mixing weeks 12 and 16 is not "at Week 16".
- Exposed to the multi-timepoint hazard for any trial reporting ACR50 at both 12 and 16.

### C. `[12, 18]`

- Smallest textual edit: lowers `min_week` only, leaving `max_week: 18` intact.
- Admits weeks 12, 16 and 18. Same estimand problem as B, over a slightly wider span.
- Textual minimality is not a statistical argument. Listed because it will be proposed.

### D. `[12, 24]`, the full endpoint window

- Admits weeks 12, 16 and 24, a 12-week span, and is the **most exposed** to the hazard above:
  PsA ACR50 is commonly reported at week 24 as well as 12 or 16, so trials contributing two
  in-window rows is likely rather than hypothetical.
- Maximises `included_study_count`, which is precisely why it warrants suspicion: it is the
  option that most resembles widening a window to make data fit.
- If chosen, it should be paired with either an explicit single-timepoint-per-trial rule or a
  code change that flags multi-timepoint contribution. Neither exists today.

## What changes if the window changes

1. **The estimand text must change with it.** It currently reads *"Relative risk of achieving
   ACR50 at Week 16"* (`analysis_protocols.yaml:116-118`). Both edits belong in one commit;
   they then cost one hash invalidation rather than two.
2. **`content_hash` changes for this protocol block only.** The hash payload is
   `{protocol_id, definition}` (`evidence/protocols.py:196-198`), so the other three protocols
   are untouched and keep any approvals they hold.
3. **Prior approvals for this protocol derive as `SUPERSEDED`.** Nothing revokes them; the
   stored hash simply stops matching. Both roles must decide again against the new content.
4. **Restart the backend.** The protocol loader is `lru_cache`'d, so an edited window has no
   effect on a running process.
5. **Any network already ratified against this protocol must be re-examined**, because its
   evidence set changes underneath the ratification.

**The approval cost is zero today and will not stay zero.** No protocol currently holds an
approval and no network is ratified, so every computed result is already `EXPLORATORY`. This
edit is cheapest to make *before* the named reviewers sign anything. That is a scheduling
fact, not an argument for any particular window.

## Caveats the decision rests on

The choice is being made against a corpus with two known open defects. State both plainly.

- **Issue 2 — 91% of outcome rows carry no canonical endpoint.** `ENDPOINT_AMBIGUOUS` on 5785
  of 6342 rows, `ENDPOINT_NOT_CANONICAL` on 247 more, roughly 310 resolving at all. Any
  per-window study count measured today is therefore a **floor, not a count**. Resolving
  Issue 2 could change what every candidate window admits, `[14, 18]` included.
- **Issue 4 — NCT03104400 is held at a stale parse.** It is one of the 12 studies verified as
  `DEV PILOT - extractions not reviewed` in order to make anything resolve at all. The week-12
  reading driving this entire decision is therefore an **unreviewed extraction of the pivotal
  trial**, and a dev-database artefact rather than a curation record. It should be re-parsed
  and genuinely verified before a methodology decision is signed on it.
- All three PsA endpoints (`PSA_ACR20_W16`, `PSA_ACR50_W16`, `PSA_PASI90_W16`) share
  `allowed_window: [12, 24]`, so a wide protocol window sits directly on top of the region
  Issue 2's ambiguity is concentrated in.

## Exact edits, none applied

Shown against the current file, for whichever option is chosen.

```yaml
# Option B — [12, 16]
  PSA_ACR50_W16_PRIMARY:
-   approved_time_window: { min_week: 14, max_week: 18 }
+   approved_time_window: { min_week: 12, max_week: 16 }
-   estimand: >-
-     Relative risk of achieving ACR50 at Week 16 versus comparator in the randomised
-     population, non-responder imputation applied.
+   estimand: >-
+     Relative risk of achieving ACR50 at the Week 12 to Week 16 assessment versus
+     comparator in the randomised population, non-responder imputation applied.
```

```yaml
# Option C — [12, 18]
-   approved_time_window: { min_week: 14, max_week: 18 }
+   approved_time_window: { min_week: 12, max_week: 18 }
```

```yaml
# Option D — [12, 24]
-   approved_time_window: { min_week: 14, max_week: 18 }
+   approved_time_window: { min_week: 12, max_week: 24 }
```

Option A is a no-op on the window plus the removal or rewording of the unsatisfiable
sensitivity rule at line 138.

Recording the decision, after the edit and a backend restart. The hash is derived server-side
and no endpoint accepts one, so the reviewer signs what is on disk and nothing else:

```http
POST /evidence-review/protocols/PSA_ACR50_W16_PRIMARY/decisions
  { "approval_role": "STATISTICAL", "decision": "APPROVED", "reviewer_id": "<name>",
    "review_note": "<the reasoning for the chosen window>" }
```

`reviewer_id` is **recorded, not authenticated** — RBAC is absent from this tree, so the
audit trail says who claimed to act, not who provably did.

## One related engineer-owned item, not part of this decision

The window logic is correct, but the **headline status for this case is misleading** and that
is ours to fix, separately. In `evidence/resolver.py`, node absence is checked before excluded
direct evidence:

```@c:\Users\SushantBandgar\Desktop\evidence-monitoring-agent\backend\app\evidence\resolver.py:394-399
    if a not in graph.nodes or b not in graph.nodes:
        missing = [t for t in (a, b) if t not in graph.nodes]
        return statuses.NETWORK_DISCONNECTED, (
            f"{', '.join(missing)} does not appear in any scoped trial, so no path of "
            "shared comparators can exist."
        )
```

Because Rinvoq's only rows are week 12, it never enters the scoped graph, so this fires and
reports the *absence of evidence* — while `evidence.unsuitable_direct` already holds
`"reports week 12, outside the approved window [14, 18]"` and `DIRECT_EVIDENCE_UNSUITABLE`
sits five checks lower at line 417. No information is lost, since Level 1 records the correct
reason in `considered`, but the headline says "no trial studied Rinvoq" when the truth is "the
trial exists and the protocol excluded it". Those are different findings and only one of them
is true here.

This is the same disclosure failure as Issue 3, reached through the resolver rather than the
builder. Tracked separately so it cannot be mistaken for a fix to the methodology question
above.

> **AMENDMENT (2026-07-27) — "No information is lost" is FALSE for a pair with no shared
> trial.** Observed on EC2, `GOVERNED`, against the `APPROVED` protocol:
>
> ```text
> GET /comparisons/resolve?...&treatment_a=Rinvoq&treatment_b=Tremfya&execution_mode=GOVERNED
>   status = NETWORK_DISCONNECTED   level = 4
>   level 1: 'No trial randomised Rinvoq against Tremfya.'
>   level 2: 'No published synthesis was found for this question.'
>   level 4: 'Rinvoq does not appear in any scoped trial, so no path of shared comparators
>             can exist.'
> ```
>
> The week-12 reason appears **nowhere in `considered`**. The mitigation above assumed Level 1
> would carry it, and for **Rinvoq vs Humira** it does — SELECT-PsA 1 holds both treatments, so
> `has_both` is true and the rejection reaches `unsuitable_direct`. For **Rinvoq vs Tremfya** no
> single trial holds both, `has_both` is false for every study, and the reason falls through to
> `scoping.skipped` where nothing surfaces it:
>
> ```@c:\Users\SushantBandgar\Desktop\evidence-monitoring-agent\backend\app\services\comparison_service.py:353-359
>         if len(usable) < 2:
>             if has_both and rejections:
>                 unsuitable_direct.extend((study.study_id, r) for r in sorted(rejections))
> ```
>
> So the governed answer to the programme's headline question asserts *the absence of Rinvoq
> evidence* while the same payload holds `NCT03104400 -> reports week 12, outside the approved
> window [14, 18]`. Those are different findings and the wrong one is the headline. The first
> sends a reader looking for trials that already exist; the second is a window conversation.
>
> **Still engineer-owned, and still not a fix to the methodology question.** But it is now a
> *governed* misstatement rather than an exploratory one, which raises its priority.
