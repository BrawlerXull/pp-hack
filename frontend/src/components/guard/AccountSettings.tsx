"use client";

import { Bell, BellOff, KeyRound, Loader2, Send, Smartphone, UserRound, Users } from "lucide-react";
import { useState, useSyncExternalStore } from "react";
import useSWR from "swr";
import { api, ApiError, type Me, type Member, type PushConfig, type PushKind, type Role } from "@/lib/api";
import { timeAgo } from "@/lib/format";
import { disablePush, enablePush, pushSupport, storedToken, type PushSupport } from "@/lib/push";
import { InstallButton } from "@/components/pwa/InstallButton";
import { Badge, Button, Card, Toggle } from "./ui";

const KINDS: { kind: PushKind; label: string; hint: string }[] = [
  { kind: "incident", label: "Attack detected", hint: "An incident opens and defences switch on." },
  { kind: "approval", label: "Needs your approval", hint: "Refunds or voids above your limits are waiting for you." },
  { kind: "dispute", label: "Dispute opened", hint: "A buyer opened a PayPal dispute; the recommendation is ready." },
  { kind: "report", label: "Attack over", hint: "Defences stood down and the incident report is ready." },
];

const noop = () => () => {};
const errorText = (e: unknown) => (e instanceof Error ? e.message : "Something went wrong");

/** The signed-in member's own settings (account, notifications) and the shop's team. */
export function AccountSettings() {
  const { data: me } = useSWR<Me>("/auth/me");
  if (!me) return null;
  return (
    <div id="account" className="scroll-mt-20 space-y-6">
      <div className="pt-4">
        <h2 className="text-lg font-semibold text-ink-1">Your account</h2>
        <p className="mt-0.5 text-sm text-ink-3">Signed in as {me.email}. These settings are yours alone.</p>
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <NotificationsCard />
        <AccountCard me={me} />
      </div>
      <TeamCard me={me} />
    </div>
  );
}

function NotificationsCard() {
  const { data: cfg, mutate } = useSWR<PushConfig>("/push/config");
  // Browser-only facts, read without a server/client mismatch on the first render.
  const support = useSyncExternalStore<PushSupport>(noop, pushSupport, () => "unsupported");
  const storedOn = useSyncExternalStore(noop, () => !!storedToken() && typeof Notification !== "undefined" && Notification.permission === "granted", () => false);
  const [override, setOn] = useState<boolean | null>(null);
  const on = override ?? storedOn;
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function run(label: string, fn: () => Promise<string | void>) {
    setBusy(label);
    setNote(null);
    try {
      const text = await fn();
      if (text) setNote({ ok: true, text });
      await mutate();
    } catch (e) {
      setNote({ ok: false, text: errorText(e) });
    } finally {
      setBusy(null);
    }
  }

  const toggleKind = (kind: PushKind, checked: boolean) =>
    run(`kind-${kind}`, async () => {
      const kinds = KINDS.map((k) => k.kind).filter((k) => (k === kind ? checked : cfg?.kinds.includes(k)));
      await api("/push/kinds", { method: "PUT", json: { kinds } });
    });

  return (
    <Card id="notifications" title="Notifications" subtitle="Get told on this device the moment something needs you" icon={Bell}>
      {!cfg ? (
        <div className="h-24 animate-pulse rounded-lg bg-gray-100" />
      ) : !cfg.enabled ? (
        <p className="text-sm text-ink-2">Push notifications aren&apos;t set up on this server yet (Firebase Cloud Messaging). Alerts still appear in the War Room.</p>
      ) : support === "ios-install-first" ? (
        <div className="space-y-3 text-sm text-ink-2">
          <p>On iPhone and iPad, notifications work once Checkout Guard is on your Home Screen (iOS 16.4 or later). Install it, open it from the Home Screen, then come back here.</p>
          <InstallButton className="inline-flex items-center gap-2 rounded-lg bg-guard px-3.5 py-2 text-sm font-semibold text-white hover:bg-guard-dark" />
        </div>
      ) : support === "unsupported" ? (
        <p className="text-sm text-ink-2">This browser can&apos;t receive push notifications. Try Chrome, Edge, Firefox or Safari 16.4+.</p>
      ) : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-gray-50 px-4 py-3">
            <div className="flex items-center gap-2.5">
              {on ? <Bell className="size-4 text-emerald-700" aria-hidden /> : <BellOff className="size-4 text-ink-3" aria-hidden />}
              <div>
                <p className="text-sm font-medium text-ink-1">{on ? "On for this device" : "Off for this device"}</p>
                <p className="text-xs text-ink-3">{on ? "Alerts arrive even when the app is closed." : "Your browser will ask for permission."}</p>
              </div>
            </div>
            <div className="flex gap-2">
              {on && (
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!!busy}
                  onClick={() =>
                    run("test", async () => {
                      await api("/push/test", { method: "POST" });
                      return "Test sent. It should appear in a few seconds.";
                    })
                  }
                >
                  {busy === "test" ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Send className="size-4" aria-hidden />} Send a test
                </Button>
              )}
              <Button
                variant={on ? "secondary" : "primary"}
                size="sm"
                disabled={!!busy}
                onClick={() =>
                  run("switch", async () => {
                    if (on) {
                      await disablePush(cfg);
                      setOn(false);
                      return "Notifications are off for this device.";
                    }
                    await enablePush(cfg);
                    setOn(true);
                    return "Notifications are on. Send a test to check.";
                  })
                }
              >
                {busy === "switch" && <Loader2 className="size-4 animate-spin" aria-hidden />} {on ? "Turn off" : "Turn on"}
              </Button>
            </div>
          </div>

          <fieldset className="space-y-4">
            <legend className="mb-3 text-sm font-medium text-ink-1">Notify me about</legend>
            {KINDS.map((k) => (
              <Toggle key={k.kind} label={k.label} hint={k.hint} checked={cfg.kinds.includes(k.kind)} disabled={!!busy} onChange={(v) => toggleKind(k.kind, v)} />
            ))}
          </fieldset>

          {cfg.devices.length > 0 && (
            <div>
              <p className="text-sm font-medium text-ink-1">Your devices</p>
              <ul className="mt-2 divide-y divide-line rounded-lg border border-line">
                {cfg.devices.map((d) => (
                  <li key={d.id} className="flex items-center gap-2.5 px-3 py-2 text-sm text-ink-2">
                    <Smartphone className="size-4 text-ink-3" aria-hidden /> {d.label || "Device"}
                    <span className="ml-auto text-xs text-ink-3">added {timeAgo(d.created_at)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      {note && (
        <p role="status" className={`mt-4 text-sm ${note.ok ? "text-emerald-800" : "text-red-700"}`}>
          {note.text}
        </p>
      )}
    </Card>
  );
}

function AccountCard({ me }: { me: Me }) {
  const [form, setForm] = useState({ current: "", next: "" });
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ ok: boolean; text: string } | null>(null);

  async function change(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setNote(null);
    try {
      await api("/auth/password", { method: "POST", json: { current: form.current, new: form.next } });
      setForm({ current: "", next: "" });
      setNote({ ok: true, text: "Password changed." });
    } catch (err) {
      setNote({ ok: false, text: errorText(err) });
    } finally {
      setBusy(false);
    }
  }

  const input =
    "mt-1.5 block w-full rounded-lg border border-line-strong bg-white px-3 py-2 text-sm text-ink-1 focus:border-guard focus:ring-2 focus:ring-guard/20 focus:outline-none";
  return (
    <Card title="Account" icon={UserRound}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">
        <dt className="text-ink-3">Name</dt>
        <dd className="text-ink-1">{me.name}</dd>
        <dt className="text-ink-3">Email</dt>
        <dd className="truncate text-ink-1">{me.email}</dd>
        <dt className="text-ink-3">Role</dt>
        <dd>
          <Badge tone={me.role === "owner" ? "info" : "neutral"}>{me.role === "owner" ? "Owner" : "Viewer"}</Badge>
        </dd>
      </dl>
      <div className="mt-5 border-t border-line pt-5">
        {me.is_demo ? (
          <p className="text-sm text-ink-3">This is the shared demo account: its password is set on the server, so it can&apos;t be changed here.</p>
        ) : (
          <form onSubmit={change} className="space-y-3">
            <p className="flex items-center gap-2 text-sm font-medium text-ink-1">
              <KeyRound className="size-4 text-ink-3" aria-hidden /> Change password
            </p>
            <label className="block text-sm text-ink-2">
              Current password
              <input type="password" autoComplete="current-password" value={form.current} onChange={(e) => setForm({ ...form, current: e.target.value })} className={input} />
            </label>
            <label className="block text-sm text-ink-2">
              New password <span className="text-ink-3">(at least 8 characters)</span>
              <input type="password" autoComplete="new-password" value={form.next} onChange={(e) => setForm({ ...form, next: e.target.value })} className={input} />
            </label>
            <Button type="submit" variant="secondary" size="sm" disabled={busy || !form.current || form.next.length < 8}>
              {busy && <Loader2 className="size-4 animate-spin" aria-hidden />} Update password
            </Button>
          </form>
        )}
        {note && (
          <p role="status" className={`mt-3 text-sm ${note.ok ? "text-emerald-800" : "text-red-700"}`}>
            {note.text}
          </p>
        )}
      </div>
    </Card>
  );
}

function TeamCard({ me }: { me: Me }) {
  const { data: team, mutate } = useSWR<Member[]>("/auth/team");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const owner = me.role === "owner";

  async function act(id: string, fn: () => Promise<unknown>) {
    setBusy(id);
    setError(null);
    try {
      await fn();
      await mutate();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : errorText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title="Team"
      subtitle="Owners change settings and approve money moves. Viewers see everything and run simulations."
      icon={Users}
      bodyClassName="p-0"
    >
      {error && (
        <p role="alert" className="border-b border-line bg-red-50 px-5 py-2 text-sm text-red-800">
          {error}
        </p>
      )}
      <ul className="divide-y divide-line">
        {(team ?? []).map((m) => {
          const locked = !owner || m.is_demo || m.id === me.id;
          return (
            <li key={m.id} className="flex flex-wrap items-center gap-3 px-5 py-3">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold text-ink-2" aria-hidden>
                {m.name.slice(0, 1).toUpperCase()}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-ink-1">
                  {m.name}
                  {m.id === me.id && <span className="font-normal text-ink-3"> (you)</span>}
                  {m.is_demo && <span className="font-normal text-ink-3"> · shared demo account</span>}
                </p>
                <p className="truncate text-xs text-ink-3">
                  {m.email} · {m.last_login_at ? `last signed in ${timeAgo(m.last_login_at)}` : "never signed in"}
                </p>
              </div>
              {locked ? (
                <Badge tone={m.role === "owner" ? "info" : "neutral"}>{m.role === "owner" ? "Owner" : "Viewer"}</Badge>
              ) : (
                <div className="flex items-center gap-2">
                  <label className="sr-only" htmlFor={`role-${m.id}`}>
                    Role for {m.name}
                  </label>
                  <select
                    id={`role-${m.id}`}
                    value={m.role}
                    disabled={busy === m.id}
                    onChange={(e) => act(m.id, () => api(`/auth/team/${m.id}`, { method: "PATCH", json: { role: e.target.value as Role } }))}
                    className="h-8 rounded-lg border border-line-strong bg-white px-2 text-sm text-ink-1"
                  >
                    <option value="owner">Owner</option>
                    <option value="viewer">Viewer</option>
                  </select>
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy === m.id}
                    onClick={() => {
                      if (window.confirm(`Remove ${m.name} from the team? They are signed out at once.`)) act(m.id, () => api(`/auth/team/${m.id}`, { method: "DELETE" }));
                    }}
                  >
                    {busy === m.id && <Loader2 className="size-4 animate-spin" aria-hidden />} Remove
                  </Button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
