"use client";

import {
  PayPalCardCvvField,
  PayPalCardExpiryField,
  PayPalCardFieldsProvider,
  PayPalCardNameField,
  PayPalCardNumberField,
  PayPalOneTimePaymentButton,
  PayPalProvider,
  usePayPal,
  usePayPalCardFields,
  usePayPalCardFieldsOneTimePaymentSession,
  INSTANCE_LOADING_STATE,
  type CardFieldsEventsOptions,
  type Components,
  type OneTimePaymentFlowResponse,
} from "@paypal/react-paypal-js/sdk-v6";
import { CheckCircle2, CreditCard, Loader2, ShieldAlert, ShieldCheck, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { api, ApiError, fetcher, type CaptureResult, type CheckoutResult, type Product, type ShopConfig } from "@/lib/api";
import { getDeviceId } from "@/lib/device";
import { Turnstile } from "./Turnstile";

type Phase =
  | { kind: "idle" }
  | { kind: "challenge" }
  | { kind: "blocked" }
  | { kind: "error"; message: string }
  | { kind: "paid"; orderId: string; captureId: string | null };

type PayMethod = "wallet" | "card";
type CardFieldsEventPayload = Parameters<CardFieldsEventsOptions["validitychange"]>[0];

// Stable arrays: the provider re-creates the SDK instance when its components change.
const WALLET_ONLY: Components[] = ["paypal-payments"];
const WALLET_AND_CARDS: Components[] = ["paypal-payments", "card-fields"];

const BANK_FAILED = "Your bank could not verify this card, so you have not been charged. Try another card or pay with PayPal.";
const CARD_PROBLEM = "We couldn't process this card, so you have not been charged. Check the card details and try again, or pay with PayPal.";

/** All required hosted fields valid (PayPal's validitychange event; the name field is optional). */
function cardFieldsValid({ data }: CardFieldsEventPayload): boolean {
  return Boolean(data?.number?.isValid && data?.expiry?.isValid && data?.cvv?.isValid);
}

/** Plain-language text for a capture that did not go through. Never shows PayPal's raw codes. */
function paymentProblem(res: Pick<CaptureResult, "outcome" | "decline_code">): string {
  if (res.outcome === "sca_failed") return BANK_FAILED;
  if (res.outcome === "sca_required") return "Your bank needs to verify this card before it can be charged. Please try again.";
  if (res.outcome === "error") return "PayPal couldn't complete the payment just now. You have not been charged; please try again in a moment.";
  if (res.outcome !== "declined") return "The payment was not completed. You have not been charged; please try again.";
  switch (res.decline_code) {
    case "INSUFFICIENT_FUNDS":
      return "Your card was declined for insufficient funds. Try another card or pay with PayPal.";
    case "EXPIRED_CARD":
    case "CARD_EXPIRED":
      return "This card has expired. Please use a different card.";
    case "CVV2_FAILURE":
    case "CVV2_FAILURE_POSSIBLE_RETRY_WITH_CVV":
    case "INVALID_SECURITY_CODE":
      return "The security code didn't match. Check the 3 digits on the back of the card and try again.";
    case "TRANSACTION_LIMIT_EXCEEDED":
      return "This payment is over your card's limit. Try another card or pay with PayPal.";
    default:
      return "Your bank declined this card. Please use a different card, pay with PayPal, or contact your bank.";
  }
}

export function CheckoutSheet({
  product,
  clientId,
  configFailed = false,
  onClose,
}: {
  product: Product;
  /** undefined while /shop/config loads; "" when the server has no PayPal client id configured. */
  clientId: string | undefined;
  configFailed?: boolean;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Card Fields need a browser-safe client token from our server (POST /shop/client-token). Without one
  // the PayPal button still works with the public client id; only card payments are switched off.
  const [clientToken, setClientToken] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    if (!clientId) return;
    let cancelled = false;
    api<{ client_token: string }>("/shop/client-token", { method: "POST" })
      .then((r) => !cancelled && setClientToken(r.client_token || null))
      .catch(() => !cancelled && setClientToken(null));
    return () => {
      cancelled = true;
    };
  }, [clientId]);

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-soot/40 backdrop-blur-[2px]" onClick={onClose}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Checkout"
        className="h-full w-full max-w-md overflow-y-auto bg-cream shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-wax px-6 py-4">
          <h2 className="text-lg font-semibold text-soot">Checkout</h2>
          <button onClick={onClose} className="rounded-full p-1.5 text-soot/60 hover:bg-wax" aria-label="Close">
            <X className="size-5" />
          </button>
        </div>
        <div className="space-y-6 px-4 py-6 sm:px-6">
          <div className="flex items-center gap-4 rounded-xl bg-white p-4 ring-1 ring-wax">
            <ProductArt sku={product.sku} className="size-16 shrink-0 rounded-lg" />
            <div className="min-w-0 flex-1">
              <p className="font-medium text-soot">{product.name}</p>
              <p className="text-sm text-soot/60">{product.description}</p>
            </div>
            <p className="text-lg font-semibold text-soot">${product.price}</p>
          </div>
          {clientId && clientToken === undefined ? (
            <p className="flex items-center gap-2 text-sm text-soot/60">
              <Loader2 className="size-4 animate-spin" /> Loading payment options…
            </p>
          ) : clientId && clientToken ? (
            <PayPalProvider clientToken={clientToken} environment="sandbox" components={WALLET_AND_CARDS} pageType="checkout">
              <CheckoutForm product={product} cardsAvailable />
            </PayPalProvider>
          ) : clientId ? (
            <PayPalProvider clientId={clientId} environment="sandbox" components={WALLET_ONLY} pageType="checkout">
              <CheckoutForm product={product} cardsAvailable={false} />
            </PayPalProvider>
          ) : configFailed ? (
            <p className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-900">Couldn&apos;t reach the shop server. Please try again in a moment.</p>
          ) : clientId === "" ? (
            <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
              <p className="font-medium">PayPal not configured: use the Demo lab (offline)</p>
              <p className="mt-1 text-amber-900/80">
                This server has no PayPal sandbox client id (<code>PAYPAL_CLIENT_ID</code>), so the real checkout is off. The guard still works: run a
                labelled offline simulation from the owner dashboard.
              </p>
              <Link href="/guard/lab" className="mt-3 inline-block font-medium underline">
                Open the Demo lab
              </Link>
            </div>
          ) : (
            <p className="flex items-center gap-2 text-sm text-soot/60">
              <Loader2 className="size-4 animate-spin" /> Loading payment options…
            </p>
          )}
          <p className="flex items-center justify-center gap-1.5 text-xs text-soot/50">
            <ShieldCheck className="size-3.5" /> Checkout protected by Checkout Guard
          </p>
        </div>
      </aside>
    </div>
  );
}

function CheckoutForm({ product, cardsAvailable }: { product: Product; cardsAvailable: boolean }) {
  const { loadingStatus, error } = usePayPal();
  const [email, setEmail] = useState("");
  const [method, setMethod] = useState<PayMethod>("wallet");
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [sca, setSca] = useState(false);
  const { data: shopConfig } = useSWR<ShopConfig>("/shop/config", fetcher);
  const [paying, setPaying] = useState(false);
  const [cardValid, setCardValid] = useState(false);
  const onValidity = useCallback((e: CardFieldsEventPayload) => setCardValid(cardFieldsValid(e)), []);
  const pendingCardOrder = useRef<string | null>(null); // the card order being submitted, until it reports back

  // Ask the guard up front so a human check appears before PayPal's popup or the card form, not after.
  useEffect(() => {
    let cancelled = false;
    api<{ decision: CheckoutResult["decision"] }>("/shop/precheck", {
      method: "POST",
      json: { sku: product.sku, method, device_id: getDeviceId(), challenge_token: challengeToken },
    })
      .then((r) => {
        if (cancelled) return;
        setSca(r.decision === "allow_with_sca");
        if (r.decision === "challenge") setPhase({ kind: "challenge" });
        else if (r.decision === "block") setPhase({ kind: "blocked" });
      })
      .catch(() => undefined); // createOrder enforces the same check anyway
    return () => {
      cancelled = true;
    };
  }, [product.sku, challengeToken, method]);

  async function createOrder(payWith: PayMethod): Promise<string> {
    setPhase({ kind: "idle" });
    try {
      // A card order carries no card data: PayPal's hosted fields send the card straight to PayPal.
      const res = await api<CheckoutResult>("/shop/orders", {
        method: "POST",
        json: { sku: product.sku, method: payWith, device_id: getDeviceId(), email: email || null, challenge_token: challengeToken },
      });
      if (!res.order_id) throw new Error(paymentProblem(res));
      setSca(res.decision === "allow_with_sca");
      return res.order_id;
    } catch (err) {
      if (err instanceof ApiError && err.status === 428) setPhase({ kind: "challenge" });
      else if (err instanceof ApiError && err.status === 403) setPhase({ kind: "blocked" });
      else if (err instanceof ApiError && err.status === 429) setPhase({ kind: "error", message: "Too many tries in a short time. Please wait a minute and try again." });
      else setPhase({ kind: "error", message: err instanceof ApiError ? "We couldn't start the payment. Please try again." : err instanceof Error ? err.message : "Something went wrong" });
      throw err;
    }
  }

  async function capture(orderId: string): Promise<CaptureResult> {
    return api<CaptureResult>(`/shop/orders/${orderId}/capture`, { method: "POST" });
  }

  /** Card Fields failed or the shopper closed the bank window: our server records the bank's answer from
   * PayPal (failed 3-D Secure) and never captures. */
  async function recordCardResult(orderId: string): Promise<CaptureResult> {
    return api<CaptureResult>(`/shop/orders/${orderId}/card-result`, { method: "POST" });
  }

  async function onApprove({ orderId }: { orderId: string }) {
    const res = await capture(orderId);
    if (res.outcome === "captured" || res.outcome === "authorized") setPhase({ kind: "paid", orderId, captureId: res.capture_id });
    else setPhase({ kind: "error", message: paymentProblem(res) });
  }

  /** Card Fields finished (with the bank's 3-D Secure window when it ran). Our server reads the bank's result
   * from PayPal and captures only if it passed. A failed or cancelled submit is only recorded, never
   * captured, so every outcome reaches the dashboard. */
  async function onCardSubmitted(resp: OneTimePaymentFlowResponse) {
    const { orderId } = resp.data;
    pendingCardOrder.current = null;
    try {
      if (resp.state !== "succeeded") {
        const res = await recordCardResult(orderId);
        setPhase({
          kind: "error",
          message: resp.state === "canceled" || res.outcome === "sca_failed" ? BANK_FAILED : CARD_PROBLEM,
        });
        return;
      }
      const res = await capture(orderId);
      if (res.outcome === "captured" || res.outcome === "authorized") setPhase({ kind: "paid", orderId, captureId: res.capture_id });
      else setPhase({ kind: "error", message: paymentProblem(res) });
    } catch {
      setPhase({ kind: "error", message: "We couldn't confirm the payment. Please try again." });
    } finally {
      setPaying(false);
    }
  }

  function onCardError() {
    setPaying(false);
    setPhase({ kind: "error", message: CARD_PROBLEM });
    const orderId = pendingCardOrder.current;
    pendingCardOrder.current = null;
    if (orderId) recordCardResult(orderId).catch(() => undefined); // best effort: keep the row honest
  }

  async function payByCard(submit: (orderId: string) => Promise<void>) {
    // Only a complete card creates an order: a typo'd Pay click must not add an attempt for this device.
    if (!cardValid) {
      setPhase({ kind: "error", message: "Please complete the card number, expiry date and CVV." });
      return;
    }
    setPaying(true);
    let orderId: string;
    try {
      orderId = await createOrder("card");
    } catch {
      setPaying(false);
      return;
    }
    pendingCardOrder.current = orderId;
    await submit(orderId); // the outcome arrives through onCardSubmitted / onCardError
  }

  async function verifyHuman(turnstileToken?: string) {
    setVerifying(true);
    try {
      const res = await api<{ challenge_token: string }>("/shop/challenge", {
        method: "POST",
        json: { device_id: getDeviceId(), turnstile_token: turnstileToken ?? null },
      });
      setChallengeToken(res.challenge_token);
      setPhase({ kind: "idle" });
    } catch {
      setPhase({ kind: "error", message: "The check didn't go through. Please try again." });
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

  const stopped = phase.kind === "challenge" || phase.kind === "blocked";
  const ready = loadingStatus === INSTANCE_LOADING_STATE.RESOLVED;

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

      <div role="tablist" aria-label="Payment method" className="grid grid-cols-2 gap-1 rounded-lg bg-wax/60 p-1">
        {(
          [
            ["wallet", "PayPal"],
            ["card", "Pay by card"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            role="tab"
            aria-selected={method === value}
            disabled={paying}
            onClick={() => {
              setMethod(value);
              setCardValid(false); // the card fields mount empty again
              setPhase({ kind: "idle" }); // the precheck for the new method re-asks the guard
            }}
            className={`inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition ${
              method === value ? "bg-white text-soot shadow-sm" : "text-soot/60 hover:text-soot"
            }`}
          >
            {value === "card" && <CreditCard className="size-4" />} {label}
          </button>
        ))}
      </div>

      {phase.kind === "challenge" && (
        <div className="rounded-xl border border-orange-200 bg-orange-50 p-4">
          <p className="flex items-center gap-2 font-medium text-orange-900">
            <ShieldAlert className="size-4" /> Quick check before you pay
          </p>
          <p className="mt-1 text-sm text-orange-900/80">
            We&apos;re seeing unusual activity on this item, so first-time shoppers are asked to confirm they&apos;re human.
          </p>
          {shopConfig?.turnstile_site_key ? (
            <div className="mt-3">
              <Turnstile
                siteKey={shopConfig.turnstile_site_key}
                onToken={(token) => verifyHuman(token)}
                onError={() => setPhase({ kind: "error", message: "The human check could not load. Please try again." })}
              />
              {verifying && (
                <p className="mt-1 flex items-center gap-1.5 text-xs text-orange-900/70">
                  <Loader2 className="size-3 animate-spin" /> Checking…
                </p>
              )}
              {shopConfig.turnstile_test_keys && (
                <p className="mt-1 text-[11px] text-orange-900/60">Demo: Cloudflare Turnstile with its test keys, so it always passes.</p>
              )}
            </div>
          ) : (
            <button
              onClick={() => verifyHuman()}
              disabled={verifying || !shopConfig}
              className="mt-3 inline-flex items-center gap-2 rounded-lg bg-orange-600 px-4 py-2 text-sm font-medium text-white hover:bg-orange-700 disabled:opacity-60"
            >
              {verifying && <Loader2 className="size-4 animate-spin" />} I&apos;m a real shopper
            </button>
          )}
        </div>
      )}
      {challengeToken && phase.kind === "idle" && (
        <p className="flex items-center gap-2 rounded-lg bg-green-50 px-3 py-2 text-sm text-green-800">
          <ShieldCheck className="size-4" /> Verified. You can pay now.
        </p>
      )}
      {phase.kind === "blocked" && (
        <p className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-900">
          This checkout can&apos;t be completed from this device right now. If you think this is a mistake,{" "}
          {shopConfig?.contact_email ? (
            <a className="font-medium underline" href={`mailto:${shopConfig.contact_email}?subject=Checkout%20blocked`}>
              email us at {shopConfig.contact_email}
            </a>
          ) : (
            "contact the shop"
          )}{" "}
          and we&apos;ll sort it out.
        </p>
      )}
      {phase.kind === "error" && (
        <p role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-900">
          {phase.message}
        </p>
      )}
      {sca && method === "card" && (
        <p className="flex items-start gap-2 text-xs text-soot/60">
          <ShieldCheck className="mt-0.5 size-3.5 shrink-0" /> Extra protection is on: your bank will ask you to verify this
          card (3-D Secure).
        </p>
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
        {ready && !stopped && method === "wallet" && (
          <PayPalOneTimePaymentButton
            createOrder={async () => ({ orderId: await createOrder("wallet") })}
            onApprove={onApprove}
            onCancel={() => setPhase({ kind: "idle" })}
            onError={() => setPhase((p) => (p.kind === "idle" ? { kind: "error", message: "PayPal couldn't complete the payment. You have not been charged." } : p))}
            type="pay"
          />
        )}
        {ready && !stopped && method === "card" && cardsAvailable && (
          <PayPalCardFieldsProvider amount={{ value: product.price, currencyCode: "USD" }} validitychange={onValidity}>
            <CardForm price={product.price} paying={paying} valid={cardValid} onPay={payByCard} onSubmitted={onCardSubmitted} onError={onCardError} />
          </PayPalCardFieldsProvider>
        )}
        {method === "card" && !cardsAvailable && (
          <p className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
            Card payments are unavailable right now. Please pay with PayPal.
          </p>
        )}
      </div>
      {method === "wallet" ? (
        <p className="text-center text-xs text-soot/50">
          Sandbox store: pay with a PayPal sandbox <em>personal</em> account. No real money moves.
        </p>
      ) : (
        <SandboxCards />
      )}
    </div>
  );
}

const FIELD_STYLE = {
  input: { fontSize: "15px", color: "#1c1917", padding: "10px 12px" },
  "input.invalid": { color: "#b91c1c" },
};
// A fixed height (as PayPal documents): with only minHeight the hosted iframe grows to its own ~150px default.
const FIELD_CONTAINER = { height: "3.25rem" };

/** PayPal-hosted card inputs (iframes): the card number, expiry and CVV never touch our page or server. */
function CardForm({
  price,
  paying,
  valid,
  onPay,
  onSubmitted,
  onError,
}: {
  price: string;
  paying: boolean;
  valid: boolean;
  onPay: (submit: (orderId: string) => Promise<void>) => void;
  onSubmitted: (resp: OneTimePaymentFlowResponse) => void;
  onError: () => void;
}) {
  const { error: fieldsError } = usePayPalCardFields();
  const { submit, submitResponse, error } = usePayPalCardFieldsOneTimePaymentSession();
  const handled = useRef<unknown>(null);

  useEffect(() => {
    if (!submitResponse || handled.current === submitResponse) return;
    handled.current = submitResponse;
    onSubmitted(submitResponse);
  }, [submitResponse, onSubmitted]);

  useEffect(() => {
    if (!error || handled.current === error) return;
    handled.current = error;
    onError();
  }, [error, onError]);

  if (fieldsError) {
    return (
      <p className="rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
        Card payments are unavailable right now. Please pay with PayPal.
      </p>
    );
  }

  return (
    <div className="space-y-1 rounded-xl bg-white p-3 ring-1 ring-wax">
      <PayPalCardNameField placeholder="Name on card" style={FIELD_STYLE} containerStyles={FIELD_CONTAINER} />
      <PayPalCardNumberField placeholder="Card number" style={FIELD_STYLE} containerStyles={FIELD_CONTAINER} />
      <div className="grid grid-cols-2 gap-2">
        <PayPalCardExpiryField placeholder="MM / YY" style={FIELD_STYLE} containerStyles={FIELD_CONTAINER} />
        <PayPalCardCvvField placeholder="CVV" style={FIELD_STYLE} containerStyles={FIELD_CONTAINER} />
      </div>
      <button
        onClick={() => onPay(submit)}
        disabled={paying || !valid}
        className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-lg bg-soot py-3 text-sm font-medium text-cream transition hover:bg-soot/90 disabled:opacity-60"
      >
        {paying && <Loader2 className="size-4 animate-spin" />} {paying ? "Processing…" : `Pay $${price}`}
      </button>
      {!valid && !paying && (
        <p className="text-center text-xs text-soot/60">Enter the card number, expiry date and CVV to pay.</p>
      )}
    </div>
  );
}

const TEST_CARDS = [
  { number: "4868719166101368", what: "3-D Secure challenge, passes" },
  { number: "4868719181895556", what: "3-D Secure challenge, fails" },
  { number: "4012888888881881", what: "Approved (no 3-D Secure in normal mode)" },
];

function SandboxCards() {
  return (
    <details className="rounded-lg border border-wax bg-white/60 px-3 py-2 text-xs text-soot/70">
      <summary className="cursor-pointer font-medium text-soot/80">Sandbox test cards (no real money moves)</summary>
      <ul className="mt-2 space-y-1">
        {TEST_CARDS.map((c) => (
          <li key={c.number} className="flex flex-wrap items-baseline justify-between gap-x-3">
            <span className="font-mono text-soot">{c.number.replace(/(\d{4})(?=\d)/g, "$1 ")}</span>
            <span>{c.what}</span>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-soot/60">Any future expiry date and any 3-digit CVV. For the bank&apos;s test page, follow its on-screen instructions.</p>
    </details>
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
