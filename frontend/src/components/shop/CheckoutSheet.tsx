"use client";

import {
  PayPalOneTimePaymentButton,
  PayPalProvider,
  usePayPal,
  INSTANCE_LOADING_STATE,
} from "@paypal/react-paypal-js/sdk-v6";
import { CheckCircle2, Loader2, ShieldAlert, ShieldCheck, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, type CheckoutResult, type Product } from "@/lib/api";
import { getDeviceId } from "@/lib/device";

type Phase =
  | { kind: "idle" }
  | { kind: "challenge" }
  | { kind: "blocked" }
  | { kind: "error"; message: string }
  | { kind: "paid"; orderId: string; captureId: string | null };

export function CheckoutSheet({
  product,
  clientId,
  configFailed = false,
  onClose,
}: {
  product: Product;
  clientId: string | undefined;
  configFailed?: boolean;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-soot/40 backdrop-blur-[2px]" onClick={onClose}>
      <aside
        className="h-full w-full max-w-md overflow-y-auto bg-cream shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        aria-label="Checkout"
      >
        <div className="flex items-center justify-between border-b border-wax px-6 py-4">
          <h2 className="text-lg font-semibold text-soot">Checkout</h2>
          <button onClick={onClose} className="rounded-full p-1.5 text-soot/60 hover:bg-wax" aria-label="Close">
            <X className="size-5" />
          </button>
        </div>
        <div className="space-y-6 px-6 py-6">
          <div className="flex items-center gap-4 rounded-xl bg-white p-4 ring-1 ring-wax">
            <ProductArt sku={product.sku} className="size-16 shrink-0 rounded-lg" />
            <div className="min-w-0 flex-1">
              <p className="font-medium text-soot">{product.name}</p>
              <p className="text-sm text-soot/60">{product.description}</p>
            </div>
            <p className="text-lg font-semibold text-soot">${product.price}</p>
          </div>
          {clientId ? (
            <PayPalProvider clientId={clientId} environment="sandbox" components={["paypal-payments"]} pageType="checkout">
              <CheckoutForm product={product} />
            </PayPalProvider>
          ) : configFailed ? (
            <p className="text-sm text-red-700">Couldn&apos;t load payment options. Is the backend running?</p>
          ) : clientId === undefined ? (
            <p className="text-sm text-soot/60">Loading payment options…</p>
          ) : (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
              <p className="font-medium">PayPal not configured: use the Demo lab (offline)</p>
              <p className="mt-1 text-amber-900/80">
                This server has no PayPal sandbox client id (<code>PAYPAL_CLIENT_ID</code>), so the real checkout
                is off. The guard still works: run a labelled offline simulation from the owner dashboard.
              </p>
              <Link href="/guard/lab" className="mt-3 inline-block font-medium underline">
                Open the Demo lab
              </Link>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function CheckoutForm({ product }: { product: Product }) {
  const { loadingStatus, error } = usePayPal();
  const [email, setEmail] = useState("");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [sca, setSca] = useState(false);

  // Ask the guard up front so a human check appears before PayPal's popup, not after it opens.
  useEffect(() => {
    let cancelled = false;
    api<{ decision: CheckoutResult["decision"] }>("/shop/precheck", {
      method: "POST",
      json: { sku: product.sku, method: "wallet", device_id: getDeviceId(), challenge_token: challengeToken },
    })
      .then((r) => {
        if (cancelled) return;
        if (r.decision === "challenge") setPhase({ kind: "challenge" });
        else if (r.decision === "block") setPhase({ kind: "blocked" });
      })
      .catch(() => undefined); // createOrder enforces the same check anyway
    return () => {
      cancelled = true;
    };
  }, [product.sku, challengeToken]);

  async function createOrder(): Promise<{ orderId: string }> {
    setPhase({ kind: "idle" });
    try {
      const res = await api<CheckoutResult>("/shop/orders", {
        method: "POST",
        json: { sku: product.sku, method: "wallet", device_id: getDeviceId(), email: email || null, challenge_token: challengeToken },
      });
      if (!res.order_id) throw new Error(res.decline_code ?? "PayPal could not create the order");
      setSca(res.decision === "allow_with_sca");
      return { orderId: res.order_id };
    } catch (err) {
      if (err instanceof ApiError && err.status === 428) setPhase({ kind: "challenge" });
      else if (err instanceof ApiError && err.status === 403) setPhase({ kind: "blocked" });
      else setPhase({ kind: "error", message: err instanceof Error ? err.message : "Something went wrong" });
      throw err;
    }
  }

  async function onApprove({ orderId }: { orderId: string }) {
    const res = await api<{ outcome: string; capture_id: string | null; decline_code: string | null }>(
      `/shop/orders/${orderId}/capture`,
      { method: "POST" },
    );
    if (res.outcome === "captured") setPhase({ kind: "paid", orderId, captureId: res.capture_id });
    else setPhase({ kind: "error", message: `Payment ${res.outcome}${res.decline_code ? ` (${res.decline_code})` : ""}` });
  }

  async function verifyHuman() {
    setVerifying(true);
    try {
      const res = await api<{ challenge_token: string }>("/shop/challenge", { method: "POST", json: { device_id: getDeviceId() } });
      setChallengeToken(res.challenge_token);
      setPhase({ kind: "idle" });
    } finally {
      setVerifying(false);
    }
  }

  if (phase.kind === "paid") {
    return (
      <div className="rounded-xl bg-white p-6 text-center ring-1 ring-wax">
        <CheckCircle2 className="mx-auto size-10 text-green-600" />
        <p className="mt-3 text-lg font-semibold text-soot">Thank you! Your order is confirmed.</p>
        <p className="mt-1 text-sm text-soot/60">PayPal order {phase.orderId}</p>
        {phase.captureId && <p className="text-xs text-soot/50">Capture {phase.captureId}</p>}
      </div>
    );
  }

  return (
    <div className="space-y-5">
      <label className="block">
        <span className="text-sm font-medium text-soot">Email for your receipt</span>
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="you@example.com"
          className="mt-1.5 w-full rounded-lg border border-wax bg-white px-3 py-2.5 text-soot outline-none focus:border-ember focus:ring-2 focus:ring-ember/20"
        />
      </label>

      {phase.kind === "challenge" && (
        <div className="rounded-xl border border-orange-200 bg-orange-50 p-4">
          <p className="flex items-center gap-2 font-medium text-orange-900">
            <ShieldAlert className="size-4" /> Quick check before you pay
          </p>
          <p className="mt-1 text-sm text-orange-900/80">
            We&apos;re seeing unusual activity on this item, so first-time shoppers are asked to confirm they&apos;re human.
          </p>
          <button
            onClick={verifyHuman}
            disabled={verifying}
            className="mt-3 inline-flex items-center gap-2 rounded-lg bg-orange-600 px-4 py-2 text-sm font-medium text-white hover:bg-orange-700 disabled:opacity-60"
          >
            {verifying && <Loader2 className="size-4 animate-spin" />} I&apos;m a real shopper
          </button>
        </div>
      )}
      {challengeToken && phase.kind === "idle" && (
        <p className="flex items-center gap-2 rounded-lg bg-green-50 px-3 py-2 text-sm text-green-800">
          <ShieldCheck className="size-4" /> Verified. You can pay with PayPal now.
        </p>
      )}
      {phase.kind === "blocked" && (
        <p className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-900">
          This checkout can&apos;t be completed from this device right now. If you think this is a mistake, contact the shop.
        </p>
      )}
      {phase.kind === "error" && (
        <p className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-900">{phase.message}</p>
      )}
      {sca && (
        <p className="text-xs text-soot/60">Extra protection is on: card payments are verified with your bank (3-D Secure).</p>
      )}

      <div className="min-h-12">
        {loadingStatus === INSTANCE_LOADING_STATE.PENDING && (
          <p className="flex items-center gap-2 text-sm text-soot/60">
            <Loader2 className="size-4 animate-spin" /> Loading PayPal…
          </p>
        )}
        {loadingStatus === INSTANCE_LOADING_STATE.REJECTED && (
          <p className="text-sm text-red-700">PayPal failed to load: {error?.message}</p>
        )}
        {loadingStatus === INSTANCE_LOADING_STATE.RESOLVED && phase.kind !== "challenge" && phase.kind !== "blocked" && (
          <PayPalOneTimePaymentButton
            createOrder={createOrder}
            onApprove={onApprove}
            onCancel={() => setPhase({ kind: "idle" })}
            onError={() => undefined /* handled in createOrder / onApprove */}
            type="pay"
          />
        )}
      </div>
      <p className="text-center text-xs text-soot/50">
        Sandbox store: pay with a PayPal sandbox <em>personal</em> account. No real money moves.
      </p>
    </div>
  );
}

const ART: Record<string, { bg: string; flame: string }> = {
  "SKU-TEALIGHT-3": { bg: "from-amber-100 to-orange-200", flame: "#f59e0b" },
  "SKU-PILLAR-18": { bg: "from-amber-200 to-amber-400", flame: "#ea580c" },
  "SKU-GIFTSET-42": { bg: "from-rose-100 to-amber-200", flame: "#dc2626" },
};

export function ProductArt({ sku, className = "" }: { sku: string; className?: string }) {
  const art = ART[sku] ?? ART["SKU-TEALIGHT-3"];
  const tall = sku === "SKU-PILLAR-18";
  const trio = sku === "SKU-GIFTSET-42";
  return (
    <div className={`relative flex items-end justify-center gap-2 overflow-hidden bg-gradient-to-br ${art.bg} ${className}`}>
      {(trio ? [0, 1, 2] : [0]).map((i) => (
        <svg key={i} viewBox="0 0 40 80" className={tall ? "h-4/5" : trio ? "h-1/2" : "h-2/5"} aria-hidden>
          <path d="M20 4c5 8 7 12 7 17a7 7 0 0 1-14 0c0-5 2-9 7-17z" fill={art.flame} opacity="0.9" />
          <rect x="19" y="26" width="2" height="6" fill="#3f3f46" />
          <rect x="6" y="32" width="28" height="48" rx="4" fill="#fffaf2" stroke="#e7d7bf" />
        </svg>
      ))}
    </div>
  );
}
