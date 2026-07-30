import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Network,
  RotateCcw,
  ScrollText,
  Send,
  ShieldAlert,
  ShieldCheck,
  Undo2,
  XCircle,
} from "lucide-react";

import {
  api,
  type EvidenceNetworkDetail,
  type EvidenceNetworkGate,
  type EvidenceNetworkList,
  type EvidenceProtocol,
  type MembershipPreview,
} from "../api/client";
import {
  Card,
  EmptyState,
  InfoTooltip,
  PageHeader,
  Select,
  Spinner,
} from "../components/ui";

const STATUS_CLS: Record<string, string> = {
  APPROVED: "bg-emerald-100 text-emerald-800",
  PENDING_APPROVAL: "bg-amber-100 text-amber-800",
  REJECTED: "bg-red-100 text-red-800",
  REVOKED: "bg-red-100 text-red-800",
  SUPERSEDED: "bg-slate-200 text-ink-light",
};

const ROLE_LABELS: Record<string, string> = {
  MEDICAL: "Medical",
  STATISTICAL: "Statistical",
};

// Listed rather than read off `role_statuses` so the buttons keep a stable order regardless of
// key order in the response. Both roles are required for APPROVED, and adding a third here
// without adding it to `approvals.APPROVAL_ROLES` would offer a decision the server refuses.
const DECIDABLE_ROLES = ["MEDICAL", "STATISTICAL"] as const;
type DecidableRole = (typeof DECIDABLE_ROLES)[number];

// Each ratification action applies from exactly one stage. That is the shape of the route set
// itself — `/submit` only ever means DRAFT -> PENDING_MEDICAL_REVIEW — not a second copy of the
// state machine. The server still enforces the ordering; this only avoids offering a button
// whose sole possible outcome is a 400.
const RATIFICATION_STEPS = [
  { from: "DRAFT", kind: "submit", label: "Submit for medical review" },
  { from: "PENDING_MEDICAL_REVIEW", kind: "medical", label: "Medical review" },
  { from: "PENDING_STATISTICAL_REVIEW", kind: "statistical", label: "Statistical review" },
] as const;

/**
 * Statistical analysis protocols, their derived approval state, and the three surfaces that
 * can change it.
 *
 * Recording a decision lives here and nowhere else. It used to live nowhere at all: the
 * routes existed and this page could only *display* an approval, so the two gates that decide
 * EXPLORATORY versus GOVERNED were unreachable from the UI entirely — every result was
 * exploratory by default rather than by judgement, which reads identically on screen.
 *
 * The original caution still holds, and is why the decision controls sit in their own bordered
 * strip beneath each protocol rather than inline with the status chips: a sign-off one
 * accidental click from a browse action is how a protocol gets approved by mistake. Reachable
 * is not the same as easy.
 *
 * **One reviewer name serves all three panels.** Three separate inputs invited three different
 * spellings of the same person across protocol approval, membership and ratification — a worse
 * audit trail than one field that is visibly the same claim throughout.
 */
export default function EvidenceGovernance() {
  const [rows, setRows] = useState<EvidenceProtocol[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reviewer, setReviewer] = useState("");

  const load = useCallback(() => {
    api
      .evidenceProtocols()
      .then(setRows)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(load, [load]);

  const approved = rows.filter((r) => r.is_approved).length;

  return (
    <div>
      <PageHeader
        title="Evidence Governance"
        subtitle="Analysis protocols, their content hash, and which approval roles have signed against it."
      />

      <div
        role="alert"
        className="mb-6 flex items-start gap-3 rounded-xl border-2 border-amber-300 bg-amber-50 p-4"
      >
        <ShieldAlert
          size={18}
          className="mt-0.5 shrink-0 text-amber-600"
          strokeWidth={2.2}
        />
        <div className="text-sm text-amber-900">
          <p className="font-bold">
            A reviewer name here is recorded, not authenticated.
          </p>
          <p className="mt-1 leading-relaxed">
            RBAC is absent from this tree, so the audit trail says who <em>claimed</em> to
            act, not who provably did. The approval model and its invariants hold today;
            enforcement attaches to these same routes once roles return.
          </p>
        </div>
      </div>

      <Card className="mb-6" title="Who is deciding">
        <input
          value={reviewer}
          onChange={(e) => setReviewer(e.target.value)}
          placeholder="e.g. A. Reviewer — recorded, not authenticated"
          className="w-full rounded-lg border border-line bg-canvas-card px-3 py-2 text-sm text-ink outline-none focus:border-brand-light sm:max-w-md"
        />
        <p className="mt-2 text-xs leading-relaxed text-ink-light">
          Required for every decision on this page, and deliberately not remembered between
          visits. A name that persists silently is one that eventually signs for someone
          else's judgement.
        </p>
      </Card>

      {error && (
        <div className="mb-4 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-16">
          <Spinner size={26} />
        </div>
      ) : !rows.length ? (
        <EmptyState
          icon={<ScrollText size={30} />}
          message="No analysis protocols defined."
        />
      ) : (
        <>
          <p className="mb-4 text-sm text-ink">
            <strong>{approved}</strong> of <strong>{rows.length}</strong> protocols carry
            both approvals against their current content. Until a protocol does, every
            result computed under it is <code>EXPLORATORY</code> and cannot feed question
            generation, scoring or recommendations.
          </p>

          <div className="space-y-3">
            {rows.map((p) => (
              <ProtocolCard
                key={p.protocol_id}
                protocol={p}
                reviewer={reviewer}
                onChanged={load}
              />
            ))}
          </div>

          <RatificationPanel reviewer={reviewer} />
          <MembershipPanel reviewer={reviewer} />

          <Card className="mt-6" title="Why editing a protocol retires its approval">
            <p className="text-sm leading-relaxed text-ink-light">
              <code>content_hash</code> is <strong>derived</strong> from the protocol's
              canonical content and is never accepted as input — a client that could name
              the content it approves could sign off on something other than what is on
              disk. Approval rows store the hash they were granted against, so editing the
              definition simply stops it matching and the status derives as{" "}
              <code>SUPERSEDED</code>. Nothing revokes anything, which is why invalidation
              cannot be forgotten.
            </p>
            <p className="mt-2 text-sm leading-relaxed text-ink-light">
              The hash tracks <em>meaning</em>, not layout: whitespace inside strings is
              collapsed before hashing, so re-wrapping a long estimand does not retire an
              approval. Changing the words does.
            </p>
          </Card>
        </>
      )}
    </div>
  );
}

/**
 * Network membership — Lifecycle 2, and the only surface that can record one.
 *
 * It lives here rather than on `EvidenceNetworks` because that page is a *read* surface and
 * a lifecycle transition must not sit one accidental click from a browse action. It is the
 * same rule that keeps verification off the study browser.
 *
 * **The narrowing warning is the point.** With nothing INCLUDED, membership narrows nothing
 * and a resolve consults every proposed study. The first inclusion binds the filter and the
 * rest stop contributing — so the consequence is shown before the decision, not inferred
 * afterwards from a study count that quietly dropped.
 */
function MembershipPanel({ reviewer }: { reviewer: string }) {
  const [networks, setNetworks] = useState<EvidenceNetworkList | null>(null);
  const [networkId, setNetworkId] = useState("");
  const [detail, setDetail] = useState<EvidenceNetworkDetail | null>(null);
  const [preview, setPreview] = useState<MembershipPreview | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evidenceNetworks()
      .then((list) => {
        setNetworks(list);
        if (list.networks?.length) setNetworkId(list.networks[0].network_id);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const load = useCallback(() => {
    if (!networkId) return;
    Promise.all([api.evidenceNetwork(networkId), api.membershipPreview(networkId)])
      .then(([d, p]) => {
        setDetail(d);
        setPreview(p);
      })
      .catch((e) => setError(String(e)));
  }, [networkId]);

  useEffect(load, [load]);

  const decide = async (studyId: string, decision: string) => {
    setBusy(studyId);
    setMessage(null);
    setError(null);
    try {
      const reason =
        decision === "EXCLUDED"
          ? window.prompt(
              "Why is this study excluded? A reason is required — an unexplained " +
                "exclusion cannot be told apart from a mistake.",
            )
          : undefined;
      if (decision === "EXCLUDED" && !reason?.trim()) {
        setBusy(null);
        return;
      }
      const result = await api.decideMembership(networkId, studyId, {
        decision,
        decided_by: reviewer.trim(),
        reason: reason ?? undefined,
      });
      setMessage(
        result.narrowing_warning ??
          `${studyId} recorded as ${result.membership_status}.`,
      );
      load();
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setBusy(null);
    }
  };

  const memberships = detail?.memberships ?? [];

  return (
    <Card className="mt-6" title="Network membership">
      <p className="mb-3 text-sm leading-relaxed text-ink-light">
        Distinct from study verification: <em>is this extraction accurate?</em> is
        universal, <em>does this study belong in THIS network?</em> is per analysis. The
        same verified study can be included in an ACR50 network and excluded from ACR20.
      </p>

      <div className="mb-4">
        <Select
          label="Network"
          value={networkId}
          options={(networks?.networks ?? []).map((n) => n.network_id)}
          onChange={setNetworkId}
        />
      </div>

      {preview && (
        <div
          className={`mb-4 flex items-start gap-2 rounded-xl border p-3 text-xs leading-relaxed ${
            preview.filter_binds
              ? "border-amber-300 bg-amber-50 text-amber-900"
              : "border-line bg-canvas text-ink-light"
          }`}
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0" strokeWidth={2.4} />
          <span>{preview.note}</span>
        </div>
      )}

      {message && (
        <div className="mb-3 rounded-lg border border-brand-light/40 bg-brand-surface p-3 text-xs text-ink">
          {message}
        </div>
      )}
      {error && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-800">
          {error}
        </div>
      )}

      {!memberships.length ? (
        <EmptyState
          icon={<Network size={26} />}
          message="This network has no proposed memberships."
        />
      ) : (
        <div className="space-y-1.5">
          {memberships.map((m: any) => (
            <div
              key={m.membership_id}
              className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-canvas px-3 py-2"
            >
              <span className="font-mono text-xs font-semibold text-ink">
                {m.registry_id ?? m.study_id}
              </span>
              {m.acronym && (
                <span className="text-xs text-ink-light">{m.acronym}</span>
              )}
              <span className="rounded-full border border-line bg-canvas-card px-2 py-0.5 text-[10px] font-bold text-ink-light">
                {m.membership_status}
              </span>
              {m.exclusion_reason && (
                <span className="text-[11px] italic text-ink-light">
                  {m.exclusion_reason}
                </span>
              )}
              <div className="ml-auto flex gap-1.5">
                {["INCLUDED", "EXCLUDED", "REQUIRES_REVIEW"].map((d) => (
                  <button
                    key={d}
                    onClick={() => decide(m.study_id, d)}
                    disabled={
                      !reviewer.trim() ||
                      busy === m.study_id ||
                      m.membership_status === d
                    }
                    className="rounded border border-line bg-canvas-card px-2 py-1 text-[10px] font-bold text-brand-dark disabled:opacity-30"
                  >
                    {d === "REQUIRES_REVIEW" ? "REVIEW" : d}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function ProtocolCard({
  protocol,
  reviewer,
  onChanged,
}: {
  protocol: EvidenceProtocol;
  reviewer: string;
  onChanged: () => void;
}) {
  const roleStatuses: Record<string, string> = protocol.role_statuses || {};
  const missing: string[] = protocol.missing_roles || [];
  const superseded = protocol.status === "SUPERSEDED";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const named = reviewer.trim();

  async function decide(
    role: DecidableRole,
    action: "APPROVED" | "REJECTED" | "REVOKE",
  ) {
    if (!named) {
      setError("A decision needs a named reviewer.");
      return;
    }

    // The server refuses both of these without an explanation, and rightly: a rejection blocks
    // every downstream computation, and a withdrawal that does not say why cannot be told apart
    // from an accident. Asking first means the refusal is not how the reviewer finds out.
    let reason: string | null = null;
    if (action !== "APPROVED") {
      reason = window.prompt(
        action === "REJECTED"
          ? `Why is ${ROLE_LABELS[role]} review rejecting ${protocol.protocol_id}? ` +
              "Required — a rejection from either role is decisive and cannot be outvoted."
          : `Why withdraw the ${ROLE_LABELS[role]} approval of ${protocol.protocol_id}? ` +
              "Required — the row is retained, so this stays answerable later.",
      );
      if (!reason?.trim()) return;
    }

    setBusy(true);
    setError(null);
    try {
      if (action === "REVOKE") {
        await api.revokeProtocolDecision(protocol.protocol_id, {
          approval_role: role,
          revoked_by: named,
          revocation_reason: reason!.trim(),
        });
      } else {
        await api.recordProtocolDecision(protocol.protocol_id, {
          approval_role: role,
          decision: action,
          reviewer_id: named,
          review_note: reason?.trim() || undefined,
        });
      }
      onChanged();
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm font-bold text-ink">
              {protocol.protocol_id}
            </span>
            <span
              className={`rounded-full px-2.5 py-1 text-[10px] font-bold ${STATUS_CLS[protocol.status] || "bg-slate-100"}`}
            >
              {String(protocol.status).replace(/_/g, " ").toLowerCase()}
            </span>
            {protocol.version != null && (
              <span className="text-[11px] text-ink-light">v{protocol.version}</span>
            )}
          </div>
          <p className="mt-1 text-xs text-ink-light">
            {protocol.indication}
            {protocol.canonical_outcome_id
              ? ` · ${protocol.canonical_outcome_id}`
              : ""}
          </p>
          <p className="mt-1 break-all font-mono text-[10px] text-ink-light">
            {protocol.content_hash}
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {Object.entries(roleStatuses).map(([role, state]) => (
            <span
              key={role}
              className={`rounded-lg px-2.5 py-1.5 text-[11px] font-bold ${STATUS_CLS[state] || "bg-slate-100 text-ink-light"}`}
            >
              {ROLE_LABELS[role] || role}: {String(state).toLowerCase()}
            </span>
          ))}
          {!Object.keys(roleStatuses).length && (
            <span className="text-xs text-ink-light">no decisions recorded</span>
          )}
        </div>
      </div>

      {superseded && (
        <div className="mt-3 flex items-start gap-2 rounded-lg bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            An approval exists, but it was granted against different content. The
            definition has changed since, so it no longer applies and the protocol is back
            to needing sign-off.
          </span>
        </div>
      )}

      {!!missing.length && !superseded && (
        <p className="mt-3 flex items-center gap-1.5 text-xs text-ink-light">
          Awaiting{" "}
          <strong className="text-ink">
            {missing.map((r) => ROLE_LABELS[r] || r).join(" and ")}
          </strong>{" "}
          sign-off
          <InfoTooltip content="Medical and statistical approvals are independent and independently revocable. A rejection from either is decisive and cannot be outvoted." />
        </p>
      )}

      {/* Fenced off from the status chips above deliberately. The chips are for reading and
          this is for signing, and the border is the only thing on screen that says so. */}
      <div className="mt-4 rounded-xl border border-dashed border-line bg-canvas p-3">
        <p className="mb-2 text-[11px] font-bold uppercase tracking-wide text-ink-light">
          Record a decision
        </p>

        {error && (
          <div className="mb-2 rounded-lg border border-red-200 bg-red-50 p-2 text-xs leading-relaxed text-red-800">
            {error}
          </div>
        )}

        <div className="flex flex-wrap gap-x-5 gap-y-2">
          {DECIDABLE_ROLES.map((role) => {
            // Only an APPROVED role has something to withdraw. Offering Revoke on a pending or
            // superseded role would produce the service's "no active decision to revoke".
            const active = roleStatuses[role] === "APPROVED";
            return (
              <div key={role} className="flex items-center gap-1.5">
                <span className="text-xs font-bold text-ink">{ROLE_LABELS[role]}</span>
                {active ? (
                  <button
                    onClick={() => decide(role, "REVOKE")}
                    disabled={!named || busy}
                    className="inline-flex items-center gap-1 rounded border border-line bg-canvas-card px-2 py-1 text-[10px] font-bold text-ink-light disabled:opacity-30"
                  >
                    <Undo2 size={11} strokeWidth={2.6} /> REVOKE
                  </button>
                ) : (
                  <>
                    <button
                      onClick={() => decide(role, "APPROVED")}
                      disabled={!named || busy}
                      className="inline-flex items-center gap-1 rounded border border-emerald-300 bg-emerald-50 px-2 py-1 text-[10px] font-bold text-emerald-800 disabled:opacity-30"
                    >
                      <CheckCircle2 size={11} strokeWidth={2.6} /> APPROVE
                    </button>
                    <button
                      onClick={() => decide(role, "REJECTED")}
                      disabled={!named || busy}
                      className="inline-flex items-center gap-1 rounded border border-red-300 bg-red-50 px-2 py-1 text-[10px] font-bold text-red-800 disabled:opacity-30"
                    >
                      <XCircle size={11} strokeWidth={2.6} /> REJECT
                    </button>
                  </>
                )}
              </div>
            );
          })}
        </div>

        <p className="mt-2 text-[11px] leading-relaxed text-ink-light">
          {!named
            ? "Enter a name above to enable these."
            : superseded
              ? "Approving now signs against the CURRENT content hash, not the one the retired approval was granted against."
              : "Recorded against the current content hash, which the server derives. Editing the definition afterwards retires the approval automatically."}
        </p>
      </div>
    </Card>
  );
}

/**
 * Network ratification — Lifecycle 3, and the gate that had no UI at all.
 *
 * Two stages in a fixed order: approving *medical* review advances to statistical review, it
 * does not ratify; only approving *statistical* review does. That ordering is what guarantees a
 * network cannot reach RATIFIED having seen only one of the two reviews, and it is enforced by
 * the state machine rather than by this panel remembering to check.
 *
 * **The gate verdict sits next to the buttons because ratifying is frequently not sufficient.**
 * Protocol approval and ratification are independent gates, not a sequence — a ratified network
 * under an unapproved protocol is still not governable. Reading those off two separate panels is
 * how someone ratifies a network, sees nothing change, and concludes the feature is broken.
 *
 * Rejection at either stage sends the network to REJECTED. That is not terminal — REJECTED, both
 * pending stages and RATIFIED all have a legal edge back to DRAFT — but taking it means withdrawing
 * a review that happened, so it goes through `reopen` with a mandatory reason rather than being a
 * quiet second attempt. SUPERSEDED is the only genuinely terminal state, and nothing writes it yet.
 */
function RatificationPanel({ reviewer }: { reviewer: string }) {
  const [networks, setNetworks] = useState<EvidenceNetworkList | null>(null);
  const [networkId, setNetworkId] = useState("");
  const [gate, setGate] = useState<EvidenceNetworkGate | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .evidenceNetworks()
      .then((list) => {
        setNetworks(list);
        if (list.networks?.length) setNetworkId(list.networks[0].network_id);
      })
      .catch((e) => setError(String(e)));
  }, []);

  // The gate carries the ratification status as well as the verdict, so one call answers both
  // "where is this network" and "is that enough".
  const load = useCallback(() => {
    if (!networkId) return;
    api
      .evidenceNetworkGate(networkId)
      .then(setGate)
      .catch((e) => setError(String(e)));
  }, [networkId]);

  useEffect(load, [load]);

  const named = reviewer.trim();
  const status: string = gate?.ratification_status ?? "";
  const step = RATIFICATION_STEPS.find((s) => s.from === status);

  async function run(kind: "submit" | "medical" | "statistical", approve: boolean) {
    if (!named) {
      setError("A decision needs a named reviewer.");
      return;
    }
    let note: string | null = null;
    if (!approve) {
      note = window.prompt(
        "Why is this network rejected? Required. A REJECTED network stops here: reviving it " +
          "means reopening it to DRAFT, which withdraws this decision on the record rather " +
          "than quietly re-running the review.",
      );
      if (!note?.trim()) return;
    }

    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const next =
        kind === "submit"
          ? await api.submitNetwork(networkId, { submitted_by: named })
          : await api.reviewNetwork(networkId, kind, {
              reviewer: named,
              approve,
              note: note?.trim() || undefined,
            });
      setMessage(
        next.is_computable
          ? `${networkId} is RATIFIED. Its membership is now frozen.`
          : `${networkId} is now ${next.ratification_status}.`,
      );
      load();
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  }

  // Reopening withdraws a review that already happened, so the reason is mandatory server-side
  // and prompted for here rather than sent empty and bounced. The prompt states the one thing
  // that is irreversible about it: the review record on the row does not survive.
  async function reopen() {
    if (!named) {
      setError("Reopening a network needs a named reviewer.");
      return;
    }
    const reason = window.prompt(
      `Why is ${networkId} being reopened? Required.\n\n` +
        "This takes it back to DRAFT so its membership and graph can change again. The " +
        "reviewer names and dates on the network are cleared \u2014 they stay in the audit log, " +
        "but the row will no longer show that anyone reviewed it. This is not a supersede: " +
        "no snapshot of the approved evidence set is kept.",
    );
    if (!reason?.trim()) return;

    setBusy(true);
    setMessage(null);
    setError(null);
    try {
      const next = await api.reopenNetwork(networkId, {
        reopened_by: named,
        reason: reason.trim(),
      });
      setMessage(
        `${networkId} is now ${next.ratification_status}. Its membership can be changed again, ` +
          "and it will need both reviews to reach RATIFIED.",
      );
      load();
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setBusy(false);
    }
  }

  const canReopen = gate?.can_reopen === true;

  const reopenButton = (
    <button
      onClick={reopen}
      disabled={!named || busy}
      className="inline-flex items-center gap-1 rounded border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-[10px] font-bold text-amber-900 disabled:opacity-30"
    >
      <RotateCcw size={11} strokeWidth={2.6} /> REOPEN TO DRAFT
    </button>
  );

  return (
    <Card className="mt-6" title="Network ratification">
      <p className="mb-3 text-sm leading-relaxed text-ink-light">
        Approving medical review advances the network to statistical review — it does{" "}
        <strong>not</strong> ratify. Only the statistical stage does, which is what stops a
        network reaching <code>RATIFIED</code> having seen one review.
      </p>

      <div className="mb-4">
        <Select
          label="Network"
          value={networkId}
          options={(networks?.networks ?? []).map((n) => n.network_id)}
          onChange={setNetworkId}
        />
      </div>

      {gate && (
        <div
          className={`mb-4 flex items-start gap-2 rounded-xl border p-3 text-xs leading-relaxed ${
            gate.may_compute_governed
              ? "border-emerald-300 bg-emerald-50 text-emerald-900"
              : "border-amber-300 bg-amber-50 text-amber-900"
          }`}
        >
          {gate.may_compute_governed ? (
            <ShieldCheck size={14} className="mt-0.5 shrink-0" strokeWidth={2.4} />
          ) : (
            <AlertTriangle size={14} className="mt-0.5 shrink-0" strokeWidth={2.4} />
          )}
          <div>
            <p className="font-bold">
              {gate.may_compute_governed
                ? "GOVERNED execution permitted"
                : `Blocked: ${String(gate.blocking_status ?? "").replace(/_/g, " ").toLowerCase()}`}
            </p>
            <p className="mt-1">{gate.reason}</p>
            <p className="mt-1 font-mono text-[10px]">
              ratification {status || "—"} · protocol {gate.protocol_id ?? "none"} (
              {gate.protocol_status ?? "—"})
            </p>
          </div>
        </div>
      )}

      {message && (
        <div className="mb-3 rounded-lg border border-brand-light/40 bg-brand-surface p-3 text-xs text-ink">
          {message}
        </div>
      )}
      {error && (
        <div className="mb-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs leading-relaxed text-red-800">
          {error}
        </div>
      )}

      {/* Shown before the decision rather than discovered after it: ratifying freezes the
          evidence set, and `decide_membership` then refuses outright. Anyone still intending to
          include or exclude a study has to do it first. */}
      {step?.kind === "statistical" && (
        <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" strokeWidth={2.4} />
          <span>
            Approving here ratifies the network and <strong>freezes its membership</strong>.
            Membership decisions are refused afterwards, because changing the evidence set a
            reviewer approved would leave it still looking approved. Finish membership below
            first — the only way back is reopening to <code>DRAFT</code>, which withdraws this
            approval on the record and loses both reviews.
          </span>
        </div>
      )}

      {step ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-bold text-ink">{step.label}</span>
          {step.kind === "submit" ? (
            <button
              onClick={() => run("submit", true)}
              disabled={!named || busy}
              className="inline-flex items-center gap-1.5 rounded-lg bg-brand-dark px-3 py-1.5 text-xs font-bold text-white disabled:opacity-30"
            >
              <Send size={12} strokeWidth={2.6} /> SUBMIT
            </button>
          ) : (
            <>
              <button
                onClick={() => run(step.kind, true)}
                disabled={!named || busy}
                className="inline-flex items-center gap-1 rounded border border-emerald-300 bg-emerald-50 px-2.5 py-1.5 text-[10px] font-bold text-emerald-800 disabled:opacity-30"
              >
                <CheckCircle2 size={11} strokeWidth={2.6} /> APPROVE
              </button>
              <button
                onClick={() => run(step.kind, false)}
                disabled={!named || busy}
                className="inline-flex items-center gap-1 rounded border border-red-300 bg-red-50 px-2.5 py-1.5 text-[10px] font-bold text-red-800 disabled:opacity-30"
              >
                <XCircle size={11} strokeWidth={2.6} /> REJECT
              </button>
            </>
          )}
          {canReopen && reopenButton}
          {!named && (
            <span className="text-[11px] text-ink-light">
              Enter a name above to enable these.
            </span>
          )}
        </div>
      ) : canReopen ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-bold text-ink">
            {status === "RATIFIED" ? "Ratified — membership is frozen" : `${status}`}
          </span>
          {reopenButton}
          {!named && (
            <span className="text-[11px] text-ink-light">
              Enter a name above to enable this.
            </span>
          )}
        </div>
      ) : (
        <p className="text-xs leading-relaxed text-ink-light">
          {status === "SUPERSEDED"
            ? "Superseded. This network is retired for good — build its replacement rather than reviving it."
            : "Select a network."}
        </p>
      )}

      {canReopen && (
        <p className="mt-2 text-[11px] leading-relaxed text-ink-light">
          Reopening returns the network to <code>DRAFT</code> so its membership and graph can
          change again, and clears the reviewer names from the row — the audit log keeps them.
          It is <strong>not</strong> a supersede: no snapshot of the approved evidence set is
          retained, so use it for an approval that should not have happened rather than for a
          set you may need to show later.
        </p>
      )}
    </Card>
  );
}
