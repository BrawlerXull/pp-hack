"use client";

import { Check, CircleSlash, Loader2, PartyPopper, Save, ShieldCheck, Sparkles, X } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { Button, Card, PageHeader, Skeleton } from "@/components/guard/ui";
import { api, type MerchantSettings } from "@/lib/api";

export default function SettingsPage() {
  const { data, mutate } = useSWR<MerchantSettings>("/settings");
  if (!data) return <Skeleton className="h-64" />;
  return <SettingsForm current={data} onSaved={(s) => mutate(s, { revalidate: false })} />;
}

function SettingsForm({ current, onSaved }: { current: MerchantSettings; onSaved: (s: MerchantSettings) => void }) {
  // The form shows the server's settings until the owner edits something. Following server changes (e.g. a
  // flash sale started from the demo bar) means a save never sends back a stale copy that erases a sale window.
  const [draft, setDraft] = useState<MerchantSettings | null>(null);
  const form = draft ?? current;
  const dirty = draft !== null;
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = <K extends keyof MerchantSettings>(k: K, v: MerchantSettings[K]) => setDraft({ ...form, [k]: v });
  const now = new Date();
  const activeSale = form.sale_windows.find((w) => new Date(w.start) <= now && now <= new Date(w.end));

  async function save(next = form) {
    const limits = [next.max_auto_refund_per_txn, next.max_auto_refund_per_incident, next.deescalate_after_min];
    if (limits.some((v) => v === "" || !Number.isFinite(Number(v)) || Number(v) < 0)) {
      setError("Limits must be numbers of 0 or more.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await api<MerchantSettings>("/settings", { method: "PUT", json: next });
      onSaved(res);
      setDraft(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } catch (e) {
      setError(e instanceof Error ? `Couldn't save: ${e.message}` : "Couldn't save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="You set the limits. Checkout Guard acts inside them, and the AI can never change them."
        action={
          <Button onClick={() => save()} disabled={saving || !dirty}>
            {saving ? <Loader2 className="size-4 animate-spin" /> : saved ? <Check className="size-4" /> : <Save className="size-4" />} {saved ? "Saved" : "Save changes"}
          </Button>
        }
      />
      {error && <p className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-200">{error}</p>}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Autopilot" icon={ShieldCheck}>
          <Toggle
            label="Act automatically"
            hint="Switch on defences and refund matched test payments without waking you. Off: refunds wait for your approval."
            checked={form.autopilot}
            onChange={(v) => set("autopilot", v)}
          />
          <div className="my-5 border-t border-white/[0.06]" />
          <Toggle
            label="Kill switch"
            hint="Turn Checkout Guard's defences off completely. It keeps watching and alerting, but takes no action."
            checked={form.kill_switch}
            onChange={(v) => set("kill_switch", v)}
            danger
          />
        </Card>

        <Card title="Safety limits" icon={CircleSlash}>
          <div className="grid gap-4 sm:grid-cols-2">
            <NumberField label="Max auto-refund per payment" prefix="$" value={form.max_auto_refund_per_txn} onChange={(v) => set("max_auto_refund_per_txn", v)} />
            <NumberField label="Max auto-refund per incident" prefix="$" value={form.max_auto_refund_per_incident} onChange={(v) => set("max_auto_refund_per_incident", v)} />
            <NumberField label="Stand down after quiet for" suffix="min" value={form.deescalate_after_min} onChange={(v) => set("deescalate_after_min", v)} />
          </div>
          <p className="mt-4 text-xs text-ink-3">Anything above these limits is queued for your approval.</p>
        </Card>

        <Card title="Sales & promotions" icon={PartyPopper}>
          <p className="text-sm text-ink-2">
            Tell Checkout Guard when you&apos;ve announced a sale, so a rush of real customers is never mistaken for an attack. During a sale it won&apos;t block or refund; at
            most it asks for 3-D Secure.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {activeSale ? (
              <>
                <span className="rounded-lg bg-sky-400/10 px-3 py-1.5 text-sm text-sky-200 ring-1 ring-sky-400/20">
                  Sale running until {new Date(activeSale.end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                  {activeSale.note && <span className="text-sky-200/70"> · {activeSale.note}</span>}
                </span>
                <Button variant="secondary" onClick={() => save({ ...form, sale_windows: [] })}>
                  End sale now
                </Button>
              </>
            ) : (
              [2, 6, 24].map((h) => (
                <Button
                  key={h}
                  variant="secondary"
                  onClick={() =>
                    save({ ...form, sale_windows: [{ start: new Date().toISOString(), end: new Date(Date.now() + h * 3600_000).toISOString(), note: `Sale announced (${h}h)` }] })
                  }
                >
                  I&apos;m running a sale: next {h}h
                </Button>
              ))
            )}
          </div>
        </Card>

        <Card title="AI guardrails" subtitle="Built in. Not configurable." icon={Sparkles}>
          <ul className="space-y-2.5 text-sm">
            {[
              [true, "Classifies unusual traffic and explains why, in plain English"],
              [true, "Sees only totals and masked rows: email domain, hashed device, no card numbers"],
              [false, "Cannot move money: refunds need the fixed rules, your caps and a matching attack signature"],
              [false, "Cannot choose which payments are refunded or change your limits"],
              [false, "Cannot block anyone during a sale you've announced"],
            ].map(([can, text]) => (
              <li key={text as string} className="flex items-start gap-2.5">
                <span className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full ${can ? "bg-emerald-400/10 text-emerald-300" : "bg-red-400/10 text-red-300"}`}>
                  {can ? <Check className="size-3" /> : <X className="size-3" />}
                </span>
                <span className="text-ink-2">{text}</span>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </>
  );
}

function Toggle({ label, hint, checked, onChange, danger }: { label: string; hint: string; checked: boolean; onChange: (v: boolean) => void; danger?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <span>
        <span className="block text-sm font-medium text-ink-1">{label}</span>
        <span className="mt-0.5 block text-xs leading-relaxed text-ink-3">{hint}</span>
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 shrink-0 rounded-full transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-guard ${
          checked ? (danger ? "bg-red-500" : "bg-guard") : "bg-white/15"
        }`}
      >
        <span className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition-all ${checked ? "left-5.5" : "left-0.5"}`} />
      </button>
    </div>
  );
}

function NumberField({ label, value, onChange, prefix, suffix }: { label: string; value: string | number; onChange: (v: string) => void; prefix?: string; suffix?: string }) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-ink-2">{label}</span>
      <div className="mt-1.5 flex items-center rounded-lg border border-white/10 bg-white/[0.03] focus-within:border-guard/60 focus-within:ring-2 focus-within:ring-guard/20">
        {prefix && <span className="pl-3 text-sm text-ink-3">{prefix}</span>}
        <input type="number" min={0} value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg bg-transparent px-3 py-2 text-sm text-ink-1 outline-none" />
        {suffix && <span className="pr-3 text-sm text-ink-3">{suffix}</span>}
      </div>
    </label>
  );
}
