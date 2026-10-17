"use client";

import { Loader2, Save } from "lucide-react";
import { useState } from "react";
import useSWR from "swr";
import { Button, Card } from "@/components/guard/ui";
import { api, type MerchantSettings } from "@/lib/api";

export default function SettingsPage() {
  const { data, mutate } = useSWR<MerchantSettings>("/settings");
  if (!data) return <div className="h-40 animate-pulse rounded-2xl bg-slate-100" />;
  return <SettingsForm initial={data} onSaved={(s) => mutate(s, { revalidate: false })} />;
}

function SettingsForm({ initial, onSaved }: { initial: MerchantSettings; onSaved: (s: MerchantSettings) => void }) {
  const [form, setForm] = useState<MerchantSettings>(initial);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const set = <K extends keyof MerchantSettings>(k: K, v: MerchantSettings[K]) => setForm({ ...form, [k]: v });
  const saleActive = form.sale_windows.some((w) => new Date(w.start) <= new Date() && new Date() <= new Date(w.end));

  async function save(next = form) {
    setSaving(true);
    try {
      const res = await api<MerchantSettings>("/settings", { method: "PUT", json: next });
      setForm(res);
      onSaved(res);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Settings</h1>
        <Button onClick={() => save()} disabled={saving}>
          {saving ? <Loader2 className="size-4 animate-spin" /> : <Save className="size-4" />} {saved ? "Saved" : "Save"}
        </Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Autopilot">
          <Toggle
            label="Act automatically"
            hint="Switch on defences and refund matched test payments without waking you. Off: refunds wait for your approval."
            checked={form.autopilot}
            onChange={(v) => set("autopilot", v)}
          />
          <div className="my-4 border-t border-slate-100" />
          <Toggle
            label="Kill switch"
            hint="Turn Checkout Guard off completely. It keeps watching and alerting, but takes no action."
            checked={form.kill_switch}
            onChange={(v) => set("kill_switch", v)}
            danger
          />
        </Card>

        <Card title="Safety limits">
          <div className="grid gap-4 sm:grid-cols-2">
            <NumberField label="Max auto-refund per payment" prefix="$" value={form.max_auto_refund_per_txn} onChange={(v) => set("max_auto_refund_per_txn", v)} />
            <NumberField label="Max auto-refund per incident" prefix="$" value={form.max_auto_refund_per_incident} onChange={(v) => set("max_auto_refund_per_incident", v)} />
            <NumberField label="Stand down after quiet for" suffix="min" value={form.deescalate_after_min} onChange={(v) => set("deescalate_after_min", Number(v))} />
          </div>
          <p className="mt-3 text-xs text-slate-500">Anything above these limits is queued for your approval. The AI can never change them.</p>
        </Card>

        <Card title="Sales & promotions" className="lg:col-span-2">
          <p className="text-sm text-slate-600">
            Tell Checkout Guard when you&apos;ve announced a sale, so a rush of real customers is never mistaken for an attack. During a sale it won&apos;t block or refund; at most it asks for 3-D Secure.
          </p>
          <div className="mt-4 flex flex-wrap items-center gap-3">
            {saleActive ? (
              <>
                <span className="rounded-md bg-blue-50 px-2.5 py-1 text-sm text-blue-800">
                  Sale running until {new Date(form.sale_windows.at(-1)!.end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                </span>
                <Button variant="secondary" onClick={() => save({ ...form, sale_windows: [] })}>End sale now</Button>
              </>
            ) : (
              [2, 6, 24].map((h) => (
                <Button
                  key={h}
                  variant="secondary"
                  onClick={() =>
                    save({
                      ...form,
                      sale_windows: [{ start: new Date().toISOString(), end: new Date(Date.now() + h * 3600_000).toISOString(), note: `Sale announced (${h}h)` }],
                    })
                  }
                >
                  I&apos;m running a sale: next {h}h
                </Button>
              ))
            )}
          </div>
        </Card>
      </div>
    </>
  );
}

function Toggle({ label, hint, checked, onChange, danger }: { label: string; hint: string; checked: boolean; onChange: (v: boolean) => void; danger?: boolean }) {
  return (
    <label className="flex cursor-pointer items-start justify-between gap-4">
      <span>
        <span className="block text-sm font-medium text-slate-800">{label}</span>
        <span className="block text-xs text-slate-500">{hint}</span>
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative h-6 w-11 shrink-0 rounded-full transition ${checked ? (danger ? "bg-red-600" : "bg-guard") : "bg-slate-300"}`}
      >
        <span className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition ${checked ? "left-5.5" : "left-0.5"}`} />
      </button>
    </label>
  );
}

function NumberField({ label, value, onChange, prefix, suffix }: { label: string; value: string | number; onChange: (v: string) => void; prefix?: string; suffix?: string }) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-slate-700">{label}</span>
      <div className="mt-1.5 flex items-center rounded-lg border border-slate-300 focus-within:border-guard focus-within:ring-2 focus-within:ring-guard/20">
        {prefix && <span className="pl-3 text-sm text-slate-500">{prefix}</span>}
        <input type="number" min={0} value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg bg-transparent px-3 py-2 text-sm outline-none" />
        {suffix && <span className="pr-3 text-sm text-slate-500">{suffix}</span>}
      </div>
    </label>
  );
}
