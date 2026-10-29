"use client";

import { Check, ExternalLink, Gavel, Loader2, Scale, Sparkles, TriangleAlert } from "lucide-react";
import { useState } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Badge, Button, Card } from "@/components/guard/ui";
import { api, type DisputeCase } from "@/lib/api";
import { money, timeAgo } from "@/lib/format";

const RECOMMEND: Record<DisputeCase["recommendation"], { label: string; tone: "good" | "info" | "warn" }> = {
  accept: { label: "Recommend: accept", tone: "info" },
  contest: { label: "Recommend: contest", tone: "good" },
  review: { label: "Your call", tone: "warn" },
};

// Why the fixed rule recommends it (app/disputes/advisor.py). Never the AI's opinion.
const RULE: Record<string, string> = {
  D1_already_refunded: "Already refunded: contest with proof of refund, so the buyer is not paid twice.",
  D2_3ds_authenticated: "The bank verified the cardholder with 3-D Secure: liability for this claim is the issuer's.",
  D3_attack_payment: "Part of a confirmed card-testing attack: the card was used without consent. Contesting cannot honestly win.",
  D4_review: "Nothing in our records decides it: check whether the order was delivered.",
};

const STATUS: Record<string, string> = {
  accepted: "Accepted at PayPal",
  contested: "Evidence sent to PayPal",
  dismissed: "Answered in PayPal",
  resolved: "Resolved by PayPal",
  failed: "PayPal did not take the answer",
};

/** Chargebacks and disputes: fixed rules recommend, the AI reads the buyer and drafts, the owner decides. */
export function DisputesCard({ incidentId }: { incidentId?: string }) {
  const { data } = useSWR<DisputeCase[]>("/dashboard/disputes", { refreshInterval: 15_000 });
  const cases = (data ?? []).filter((c) => !incidentId || c.incident_id === incidentId);
  if (!cases.length) return null;
  // A failed answer (PayPal refused it) stays open for another try.
  const open = cases.filter((c) => c.status === "open" || c.status === "failed");
  return (
    <Card
      title="Chargebacks & disputes"
      subtitle="Fixed rules recommend from our records, the AI reads the buyer and drafts the note, you decide. PayPal's Disputes API gets the answer."
      icon={Gavel}
    >
      <ul className="space-y-3">
        {open.map((c) => (
          <OpenCase key={c.id} c={c} />
        ))}
        {cases
          .filter((c) => c.status !== "open" && c.status !== "failed")
          .slice(0, 5)
          .map((c) => (
            <li key={c.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-white/[0.03] px-3 py-2 text-xs text-ink-2">
              <span>
                {money(c.amount)} · {(c.reason ?? "dispute").replace(/_/g, " ").toLowerCase()} · <code className="font-mono">{c.id}</code>
              </span>
              <Badge tone={c.status === "failed" ? "bad" : "neutral"}>
                {STATUS[c.status] ?? c.status}
                {c.paypal_live ? "" : " (simulated)"}
              </Badge>
            </li>
          ))}
      </ul>
    </Card>
  );
}

function OpenCase({ c }: { c: DisputeCase }) {
  const { mutate } = useSWRConfig();
  const [note, setNote] = useState(c.note);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const rec = RECOMMEND[c.recommendation];
  async function answer(decision: "accept" | "contest" | "dismiss") {
    setBusy(decision);
    setError(null);
    try {
      await api(`/dashboard/disputes/${c.id}/${decision}`, { method: "POST", json: { note: note !== c.note ? note : null } });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong");
    } finally {
      setBusy(null);
      mutate((k) => typeof k === "string" && k.startsWith("/dashboard"));
    }
  }
  return (
    <li className="rounded-xl border border-white/[0.07] bg-white/[0.02] p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium text-ink-1">
            {money(c.amount)} · {(c.reason ?? "dispute").replace(/_/g, " ").toLowerCase()}
            {!c.paypal_live && <span className="ml-2 rounded bg-violet-400/15 px-1.5 py-0.5 text-[10px] font-semibold text-violet-200">SIM</span>}
          </p>
          <p className="text-xs text-ink-3">
            <code className="font-mono">{c.id}</code> · opened {timeAgo(c.created_at)}
            {c.response_due && ` · answer by ${new Date(c.response_due).toLocaleDateString()}`}
          </p>
        </div>
        <Badge tone={rec.tone}>{rec.label}</Badge>
      </div>
      <p className="mt-2 flex items-start gap-1.5 text-xs text-ink-2">
        <Scale className="mt-0.5 size-3.5 shrink-0 text-sky-300" /> {RULE[c.rule] ?? c.rule}
      </p>
      {c.buyer_says && (
        <p className={`mt-1.5 flex items-start gap-1.5 text-xs ${c.contradicts_reason ? "text-amber-200" : "text-ink-2"}`}>
          {c.contradicts_reason ? <TriangleAlert className="mt-0.5 size-3.5 shrink-0" /> : <Sparkles className="mt-0.5 size-3.5 shrink-0 text-violet-300" />}
          <span>
            <span className="text-ink-3">The buyer says (read by the AI):</span> {c.buyer_says}
          </span>
        </p>
      )}
      <label className="mt-3 block text-[11px] text-ink-3">
        Note to PayPal · drafted by {c.note_by === "template" ? "our template" : c.note_by === "owner" ? "you" : c.note_by}, numbers from our records
        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={3}
          className="mt-1 w-full rounded-lg border border-white/10 bg-night-750 px-2.5 py-2 text-xs leading-relaxed text-ink-1 focus:border-guard focus:outline-none"
        />
      </label>
      {c.status === "failed" && (
        <p className="mt-1 text-xs text-red-300">PayPal did not take the last answer: {c.paypal_result}. Edit and send again.</p>
      )}
      {error && <p className="mt-1 text-xs text-red-300">{error}</p>}
      <div className="mt-2 flex flex-wrap gap-1.5">
        {(["accept", "contest"] as const).map((d) => (
          <Button key={d} size="sm" variant={c.recommendation === d ? "primary" : "secondary"} disabled={!!busy} onClick={() => answer(d)}>
            {busy === d ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />} {d === "accept" ? "Accept claim" : "Contest with evidence"}
          </Button>
        ))}
        <Button size="sm" variant="ghost" disabled={!!busy} onClick={() => answer("dismiss")}>
          <ExternalLink className="size-3.5" /> I&apos;ll answer in PayPal
        </Button>
      </div>
    </li>
  );
}
