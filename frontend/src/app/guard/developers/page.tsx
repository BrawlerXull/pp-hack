"use client";

import { Check, Copy, ExternalLink, KeyRound, Loader2, PlugZap, Send, Trash2, TriangleAlert } from "lucide-react";
import { useId, useState, useSyncExternalStore } from "react";
import useSWR from "swr";
import { Badge, Button, Card, LiveDot, PageHeader, Skeleton } from "@/components/guard/ui";
import { api, ApiError, type ApiKeyRow, type IntegrationStatus, type Me, type NewApiKey } from "@/lib/api";
import { timeAgo } from "@/lib/format";

const REPO_SDKS = "https://github.com/BrawlerXull/pp-hack/tree/main/sdks";
const KEY_PLACEHOLDER = "cg_sk_your_key";

// Where a shop's server reaches this deployment: the dashboard's own origin, through the /api proxy.
const noop = () => () => {};
function useApiBase(): string {
  return useSyncExternalStore(
    noop,
    () => `${window.location.origin}/api`,
    () => "https://your-checkout-guard.example/api",
  );
}

export default function DevelopersPage() {
  const { data: me } = useSWR<Me>("/auth/me");
  // A key made on this visit: the snippets and the test call use it (it is never shown again after a reload).
  const [fresh, setFresh] = useState<NewApiKey | null>(null);
  const base = useApiBase();
  const key = fresh?.key ?? KEY_PLACEHOLDER;

  return (
    <>
      <PageHeader
        title="Connect your store"
        subtitle="Checkout Guard works with any checkout that takes PayPal. Your server asks it about each checkout before creating the PayPal order: one call, about 15 lines of code."
      />
      <ConnectionCard />
      <KeysCard owner={me?.role === "owner"} fresh={fresh} onCreated={setFresh} />
      <QuickstartCard base={base} apiKey={key} />
      <TryItCard base={base} apiKey={fresh?.key} />
      <SdkCard base={base} />
    </>
  );
}

/** Live: turns green as soon as the shop's server makes its first call with one of the keys. */
function ConnectionCard() {
  const { data } = useSWR<IntegrationStatus>("/integration/status", { refreshInterval: 5000 });
  if (!data) return <Skeleton className="h-24" />;
  const connected = Boolean(data.last_call_at);
  return (
    <Card title="Connection" icon={PlugZap}>
      <div className="flex flex-wrap items-center gap-x-8 gap-y-3" aria-live="polite">
        <p className="flex items-center gap-2 text-sm font-medium text-ink-1">
          <LiveDot tone={connected ? "good" : "neutral"} />
          {connected ? `Connected · last call ${timeAgo(data.last_call_at!)}` : "Waiting for your first call"}
        </p>
        <p className="text-sm text-ink-2">
          <span className="font-semibold text-ink-1 tabular-nums">{data.checks_24h}</span> checkouts checked in the last 24 hours
        </p>
        {data.latest_check && (
          <p className="text-sm text-ink-2">
            Latest: <span className="font-mono text-[13px]">{data.latest_check.sku}</span> →{" "}
            <Badge tone={data.latest_check.decision === "block" ? "bad" : data.latest_check.decision === "allow" ? "good" : "warn"}>
              {data.latest_check.decision.replaceAll("_", " ")}
            </Badge>{" "}
            {timeAgo(data.latest_check.ts)}
          </p>
        )}
      </div>
      {!connected && <p className="mt-3 text-[13px] text-ink-3">Create a key below, then send a test check: this turns green within a few seconds.</p>}
    </Card>
  );
}

function KeysCard({ owner, fresh, onCreated }: { owner: boolean; fresh: NewApiKey | null; onCreated: (k: NewApiKey) => void }) {
  const { data: keys, mutate } = useSWR<ApiKeyRow[]>("/integration/keys");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameId = useId();

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const made = await api<NewApiKey>("/integration/keys", { method: "POST", json: { name: name.trim() } });
      onCreated(made);
      setName("");
      await mutate();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't create the key. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(k: ApiKeyRow) {
    if (!window.confirm(`Revoke “${k.name}”? Checkouts using it stop being checked at once.`)) return;
    try {
      await api(`/integration/keys/${k.id}`, { method: "DELETE" });
      await mutate();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't revoke the key.");
    }
  }

  return (
    <Card title="API keys" icon={KeyRound} subtitle="Your server sends one in the X-Guard-Key header. Keep it secret: never put it in a web page or app.">
      {fresh && (
        <div className="mb-5 rounded-lg border border-amber-300 bg-amber-50 p-4" role="status">
          <p className="flex items-center gap-2 text-sm font-semibold text-amber-900">
            <TriangleAlert className="size-4" aria-hidden /> Copy “{fresh.name}” now. You won&apos;t see it again.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-md border border-amber-200 bg-white px-3 py-2 font-mono text-[13px] text-ink-1">{fresh.key}</code>
            <CopyButton text={fresh.key} label="Copy key" />
          </div>
          <p className="mt-2 text-[13px] text-amber-900">The code below and the test call on this page already use it.</p>
        </div>
      )}
      {!keys ? (
        <Skeleton className="h-16" />
      ) : keys.length === 0 ? (
        <p className="text-sm text-ink-3">No keys yet.</p>
      ) : (
        <ul className="divide-y divide-line rounded-lg border border-line">
          {keys.map((k) => (
            <li key={k.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-ink-1">{k.name}</p>
                <p className="text-[13px] text-ink-3">
                  <span className="font-mono">{k.prefix}…</span> · created {timeAgo(k.created_at)} ·{" "}
                  {k.last_used_at ? `last used ${timeAgo(k.last_used_at)}` : "never used"}
                </p>
              </div>
              {owner && (
                <Button variant="ghost" size="sm" onClick={() => revoke(k)} aria-label={`Revoke ${k.name}`}>
                  <Trash2 className="size-4" aria-hidden /> Revoke
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
      {owner ? (
        <form onSubmit={create} className="mt-4 flex flex-wrap items-end gap-2">
          <div className="min-w-48 flex-1">
            <label htmlFor={nameId} className="text-sm font-medium text-ink-2">
              New key name
            </label>
            <input
              id={nameId}
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={80}
              placeholder="e.g. Live store, Staging"
              className="mt-1.5 block w-full rounded-lg border border-line-strong bg-white px-3 py-2 text-sm text-ink-1 placeholder:text-ink-3 focus:border-guard focus:ring-2 focus:ring-guard/20 focus:outline-none"
            />
          </div>
          <Button type="submit" disabled={busy || !name.trim()}>
            {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <KeyRound className="size-4" aria-hidden />} Create key
          </Button>
        </form>
      ) : (
        <p className="mt-4 text-[13px] text-ink-3">Only an owner can create or revoke keys.</p>
      )}
      {error && (
        <p role="alert" className="mt-3 text-sm text-red-700">
          {error}
        </p>
      )}
    </Card>
  );
}

type Lang = "curl" | "node" | "python" | "php";
const LANGS: { id: Lang; label: string }[] = [
  { id: "node", label: "Node.js" },
  { id: "python", label: "Python" },
  { id: "php", label: "PHP" },
  { id: "curl", label: "curl" },
];

function checkSnippet(lang: Lang, base: string, key: string): string {
  switch (lang) {
    case "node":
      return `// On your server, before creating the PayPal order (Node 18+).
import { createHash } from "node:crypto";

const guard = await fetch("${base}/v1/guard/check", {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Guard-Key": process.env.CHECKOUT_GUARD_KEY }, // ${key}
  body: JSON.stringify({
    sku: item.sku,
    amount: item.price,
    method: "card", // or "wallet" for the PayPal button
    // Any stable hash of the buyer's device: we never see the raw values.
    fingerprint_hash: createHash("sha256").update(req.ip + req.headers["user-agent"]).digest("hex"),
    client_ip: req.ip,
    email_domain: email.split("@")[1], // the domain only, never the address
  }),
  signal: AbortSignal.timeout(1000), // no answer in 1 s? go on as "allow"
}).then((r) => r.json());

if (guard.decision === "block") return res.status(403).json({ error: "We couldn't take this payment." });

const order = {
  intent: "CAPTURE",
  purchase_units: [{ amount: { currency_code: "USD", value: item.price }, custom_id: guard.custom_id }],
  ...(guard.sca_method && {
    payment_source: { card: { attributes: { verification: { method: guard.sca_method } } } },
  }),
};`;
    case "python":
      return `# On your server, before creating the PayPal order.
import hashlib, os, httpx

guard = httpx.post(
    "${base}/v1/guard/check",
    headers={"X-Guard-Key": os.environ["CHECKOUT_GUARD_KEY"]},  # ${key}
    json={
        "sku": item.sku,
        "amount": str(item.price),
        "method": "card",  # or "wallet" for the PayPal button
        # Any stable hash of the buyer's device: we never see the raw values.
        "fingerprint_hash": hashlib.sha256(f"{ip}{user_agent}".encode()).hexdigest(),
        "client_ip": ip,
        "email_domain": email.split("@")[1],  # the domain only, never the address
    },
    timeout=1.0,  # no answer in 1 s? go on as "allow"
).json()

if guard["decision"] == "block":
    raise PaymentRefused("We couldn't take this payment.")

order = {
    "intent": "CAPTURE",
    "purchase_units": [{"amount": {"currency_code": "USD", "value": str(item.price)},
                        "custom_id": guard["custom_id"]}],
}
if guard["sca_method"]:
    order["payment_source"] = {"card": {"attributes": {"verification": {"method": guard["sca_method"]}}}}`;
    case "php":
      return `<?php
// On your server, before creating the PayPal order.
$ch = curl_init("${base}/v1/guard/check");
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT_MS => 1000, // no answer in 1 s? go on as "allow"
    CURLOPT_HTTPHEADER => ["Content-Type: application/json", "X-Guard-Key: " . getenv("CHECKOUT_GUARD_KEY")], // ${key}
    CURLOPT_POSTFIELDS => json_encode([
        "sku" => $item["sku"],
        "amount" => $item["price"],
        "method" => "card", // or "wallet" for the PayPal button
        "fingerprint_hash" => hash("sha256", $_SERVER["REMOTE_ADDR"] . $_SERVER["HTTP_USER_AGENT"]),
        "client_ip" => $_SERVER["REMOTE_ADDR"],
        "email_domain" => substr(strrchr($email, "@"), 1), // the domain only
    ]),
]);
$guard = json_decode(curl_exec($ch) ?: '{"decision":"allow","custom_id":null,"sca_method":null}', true);

if ($guard["decision"] === "block") { http_response_code(403); exit("We couldn't take this payment."); }

$order = ["intent" => "CAPTURE", "purchase_units" => [[
    "amount" => ["currency_code" => "USD", "value" => $item["price"]],
    "custom_id" => $guard["custom_id"],
]]];
if ($guard["sca_method"]) {
    $order["payment_source"] = ["card" => ["attributes" => ["verification" => ["method" => $guard["sca_method"]]]]];
}`;
    case "curl":
      return `curl ${base}/v1/guard/check \\
  -H "Content-Type: application/json" \\
  -H "X-Guard-Key: ${key}" \\
  -d '{"sku": "TEA-3", "amount": "3.00", "method": "card",
       "fingerprint_hash": "9f86d081884c7d659a2feaa0c55ad015", "email_domain": "gmail.com"}'

# {"decision": "allow_with_sca", "reason": "...", "sca_method": "SCA_ALWAYS",
#  "attempt_id": "k3Jx9...", "custom_id": "wick-and-wax:k3Jx9...", "incident_id": "..."}`;
  }
}

function outcomeSnippet(base: string, key: string): string {
  return `curl ${base}/v1/guard/outcome \\
  -H "Content-Type: application/json" \\
  -H "X-Guard-Key: ${key}" \\
  -d '{"attempt_id": "k3Jx9...", "outcome": "declined", "decline_code": "INSTRUMENT_DECLINED"}'

# outcome: captured | declined | error | sca_failed | abandoned`;
}

const DECISIONS: { decision: string; tone: "good" | "warn" | "bad"; what: string }[] = [
  { decision: "allow", tone: "good", what: "Create the order as usual." },
  { decision: "allow_with_sca", tone: "warn", what: "Create it with 3-D Secure: put sca_method in payment_source.card.attributes.verification.method." },
  { decision: "challenge", tone: "warn", what: "Ask the buyer to pass a human check (CAPTCHA) first. No human check on your site? Treat it as block." },
  { decision: "block", tone: "bad", what: "Don't create the order. Show a polite message with a way to contact you." },
];

function QuickstartCard({ base, apiKey }: { base: string; apiKey: string }) {
  const [lang, setLang] = useState<Lang>("node");
  const tabsId = useId();
  const code = checkSnippet(lang, base, apiKey);
  return (
    <Card title="Quickstart" subtitle="Three steps. Your checkout keeps working if Checkout Guard is slow or down: give the call a 1-second timeout and go on.">
      <ol className="space-y-6">
        <li>
          <h3 className="text-sm font-semibold text-ink-1">1. Ask before you create the PayPal order</h3>
          <div role="tablist" aria-label="Language" className="mt-3 flex flex-wrap gap-1 rounded-lg bg-gray-100 p-1 ring-1 ring-line sm:inline-flex">
            {LANGS.map((l) => (
              <button
                key={l.id}
                id={`${tabsId}-${l.id}`}
                role="tab"
                type="button"
                aria-selected={lang === l.id}
                aria-controls={`${tabsId}-panel`}
                onClick={() => setLang(l.id)}
                className={`rounded-md px-3 py-1.5 text-[13px] font-medium ${lang === l.id ? "bg-white text-ink-1 shadow-sm" : "text-ink-2 hover:text-ink-1"}`}
              >
                {l.label}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`${tabsId}-panel`} aria-labelledby={`${tabsId}-${lang}`} className="mt-2">
            <CodeBlock code={code} />
          </div>
        </li>
        <li>
          <h3 className="text-sm font-semibold text-ink-1">2. Do what the answer says</h3>
          <dl className="mt-3 divide-y divide-line rounded-lg border border-line">
            {DECISIONS.map((d) => (
              <div key={d.decision} className="flex flex-col gap-1 px-4 py-2.5 sm:flex-row sm:items-center sm:gap-4">
                <dt className="w-36 shrink-0">
                  <Badge tone={d.tone}>{d.decision}</Badge>
                </dt>
                <dd className="text-sm text-ink-2">{d.what}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-[13px] text-ink-3">
            Always put <code className="font-mono">custom_id</code> on the order: PayPal&apos;s webhooks for that order then reach this checkout, so refunds and dispute help work.
          </p>
        </li>
        <li>
          <h3 className="text-sm font-semibold text-ink-1">3. Tell it what PayPal did</h3>
          <p className="mt-1 text-sm text-ink-2">
            For what PayPal sends no webhook about (a card refused while creating the order, an error, the buyer leaving), or if your PayPal app has no webhooks set up:
          </p>
          <div className="mt-2">
            <CodeBlock code={outcomeSnippet(base, apiKey)} />
          </div>
        </li>
      </ol>
    </Card>
  );
}

/** A dry run of the real public endpoint from this page: proves the key works and lights up "Connected". */
function TryItCard({ base, apiKey }: { base: string; apiKey: string | undefined }) {
  const [pasted, setPasted] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const keyId = useId();
  const key = apiKey ?? pasted.trim();

  async function send() {
    setBusy(true);
    setResult(null);
    try {
      const res = await fetch(`${base}/v1/guard/check`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Guard-Key": key },
        body: JSON.stringify({ sku: "TEA-3", amount: "3.00", method: "card", fingerprint_hash: "developers-page-test", dry_run: true }),
      });
      const body = await res.json().catch(() => null);
      setResult({ ok: res.ok, text: res.ok ? JSON.stringify(body, null, 2) : `HTTP ${res.status}: ${body?.detail ?? "error"}` });
    } catch {
      setResult({ ok: false, text: "Couldn't reach Checkout Guard." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Send a test check" icon={Send} subtitle="A dry run: the guard answers as it would for a real checkout, and records nothing.">
      <div className="flex flex-wrap items-end gap-2">
        {apiKey ? (
          <p className="flex-1 text-sm text-ink-2">Uses the key you just created.</p>
        ) : (
          <div className="min-w-56 flex-1">
            <label htmlFor={keyId} className="text-sm font-medium text-ink-2">
              API key
            </label>
            <input
              id={keyId}
              type="password"
              autoComplete="off"
              value={pasted}
              onChange={(e) => setPasted(e.target.value)}
              placeholder="cg_sk_…"
              className="mt-1.5 block w-full rounded-lg border border-line-strong bg-white px-3 py-2 font-mono text-sm text-ink-1 placeholder:text-ink-3 focus:border-guard focus:ring-2 focus:ring-guard/20 focus:outline-none"
            />
          </div>
        )}
        <Button onClick={send} disabled={busy || !key}>
          {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Send className="size-4" aria-hidden />} Send test check
        </Button>
      </div>
      {result && (
        <div className="mt-4" aria-live="polite">
          <p className={`mb-1.5 flex items-center gap-1.5 text-sm font-medium ${result.ok ? "text-emerald-700" : "text-red-700"}`}>
            {result.ok ? <Check className="size-4" aria-hidden /> : <TriangleAlert className="size-4" aria-hidden />}
            {result.ok ? "It works. Checkout Guard answered:" : "That didn't work."}
          </p>
          <CodeBlock code={result.text} />
        </div>
      )}
    </Card>
  );
}

function SdkCard({ base }: { base: string }) {
  return (
    <Card title="SDKs and API reference" subtitle="Prefer a library? Node.js, Python and PHP clients are generated with APIMatic from this deployment's OpenAPI spec.">
      <div className="flex flex-wrap gap-2">
        <a href={REPO_SDKS} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-sm font-medium text-ink-1 hover:bg-gray-50">
          SDKs on GitHub <ExternalLink className="size-3.5" aria-hidden />
        </a>
        <a href={`${base}/v1/openapi.json`} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-sm font-medium text-ink-1 hover:bg-gray-50">
          OpenAPI spec <ExternalLink className="size-3.5" aria-hidden />
        </a>
      </div>
    </Card>
  );
}

function CodeBlock({ code }: { code: string }) {
  return (
    <div className="relative">
      {/* Focusable, so keyboard users can scroll a long snippet. */}
      <pre tabIndex={0} aria-label="Code" className="max-h-[420px] overflow-auto focus-visible:outline-2 focus-visible:outline-guard rounded-lg bg-gray-900 p-4 pr-24 text-[13px] leading-relaxed text-gray-100">
        <code>{code}</code>
      </pre>
      <div className="absolute top-2 right-2">
        <CopyButton text={code} label="Copy" dark />
      </div>
    </div>
  );
}

function CopyButton({ text, label, dark }: { text: string; label: string; dark?: boolean }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        await navigator.clipboard.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
      className={`inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium ${
        dark ? "bg-white/10 text-white hover:bg-white/20" : "border border-line bg-white text-ink-1 hover:bg-gray-50"
      }`}
    >
      {done ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
      <span aria-live="polite">{done ? "Copied" : label}</span>
    </button>
  );
}
