import {
  ArrowRight,
  Ban,
  BellRing,
  Check,
  CircleDollarSign,
  CreditCard,
  Eye,
  FileText,
  FlaskConical,
  Gavel,
  Lock,
  Moon,
  PartyPopper,
  Radio,
  RefreshCcw,
  ShieldCheck,
  Sparkles,
  Store,
  Bug,
  Webhook,
  type LucideIcon,
} from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: { absolute: "Checkout Guard · The night guard for your PayPal checkout" },
};

const NIGHT: { time: string; icon: LucideIcon; tone: string; title: string; text: string }[] = [
  { time: "02:14", icon: Eye, tone: "text-sky-300 bg-sky-400/10", title: "Watch", text: "Hundreds of $3 orders on the cheapest item in minutes. Each one looks normal on its own." },
  { time: "02:14", icon: Sparkles, tone: "text-violet-300 bg-violet-400/10", title: "Judge", text: "Gemini: card-testing attack, not a sales rush. New card on every attempt, fraud-coded declines." },
  { time: "02:15", icon: Gavel, tone: "text-emerald-300 bg-emerald-400/10", title: "Act", text: "3-D Secure on every card. Bot devices blocked. Test charges refunded before they turn into disputes." },
  { time: "07:30", icon: FileText, tone: "text-amber-300 bg-amber-400/10", title: "Explain", text: "One calm message and a morning report Meera can forward to PayPal." },
];

const STEPS: { icon: LucideIcon; name: string; who: string; text: string }[] = [
  { icon: Eye, name: "Watch", who: "Deterministic detector", text: "Every checkout passes the guard before a PayPal order is created. PayPal webhooks report every capture, refund and dispute." },
  { icon: Sparkles, name: "Judge", who: "Gemini AI analyst", text: "Called only when something looks wrong. Decides: card testing, stolen-account testing, a genuine rush, or a PayPal outage, and shows its evidence." },
  { icon: Gavel, name: "Act", who: "Fixed policy rules", text: "Graduated defences within the owner's limits: force 3-D Secure, block bot devices, refund matched test charges. Relaxes automatically." },
  { icon: FileText, name: "Explain", who: "Plain-English reports", text: "An alert at the time, a morning report, and an evidence summary for PayPal support. Every number comes from our records, not the AI." },
];

const PAYPAL: { icon: LucideIcon; name: string; text: string }[] = [
  { icon: CreditCard, name: "JS SDK v6 checkout", text: "The demo shop pays with PayPal's v6 one-time payment button." },
  { icon: Store, name: "Orders v2", text: "Create and capture, each call idempotent with PayPal-Request-Id." },
  { icon: Lock, name: "3-D Secure", text: "Card orders carry verification.method SCA_ALWAYS while under attack." },
  { icon: CircleDollarSign, name: "Refunds", text: "Payments v2 refunds for matched test charges, inside the owner's caps." },
  { icon: Webhook, name: "Webhooks", text: "Every event verified with verify-webhook-signature before it's trusted." },
  { icon: Radio, name: "Dispute events", text: "Dispute webhooks flag the original checkout attempt they came from." },
];

export default function Landing() {
  return (
    <div className="night min-h-screen overflow-x-hidden">
      <header className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
        <Link href="/" className="flex items-center gap-2.5">
          <span className="relative flex size-9 items-center justify-center rounded-xl bg-gradient-to-br from-guard to-indigo-600">
            <ShieldCheck className="size-[18px] text-white" />
            <Moon className="absolute -top-1 -right-1 size-3 fill-amber-200 text-amber-200" />
          </span>
          <span className="font-semibold tracking-tight text-ink-1">Checkout Guard</span>
        </Link>
        <nav className="flex items-center gap-1 text-sm sm:gap-2">
          <a href="#how" className="hidden rounded-lg px-3 py-2 text-ink-2 hover:text-ink-1 sm:inline">How it works</a>
          <a href="#paypal" className="hidden rounded-lg px-3 py-2 text-ink-2 hover:text-ink-1 sm:inline">PayPal</a>
          <Link href="/shop" className="rounded-lg px-3 py-2 text-ink-2 hover:text-ink-1">Demo shop</Link>
          <Link href="/guard" className="rounded-lg bg-white/[0.08] px-3.5 py-2 font-medium text-ink-1 ring-1 ring-white/10 hover:bg-white/[0.12]">
            War Room
          </Link>
        </nav>
      </header>

      {/* Hero */}
      <section className="mx-auto grid max-w-6xl items-center gap-12 px-6 pt-10 pb-20 lg:grid-cols-[1.1fr_1fr] lg:pt-16">
        <div>
          <p className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.04] px-3 py-1 text-xs text-ink-2">
            <span className="size-1.5 rounded-full bg-emerald-400" /> Built for the PayPal AI Hackathon · runs on the PayPal sandbox
          </p>
          <h1 className="mt-6 text-4xl leading-[1.05] font-semibold tracking-tight text-white sm:text-6xl">
            The night guard for your <span className="bg-gradient-to-r from-sky-300 to-guard bg-clip-text text-transparent">PayPal checkout.</span>
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-relaxed text-ink-2">
            Card-testing bots use small shops&apos; checkouts to find out which stolen cards still work. Checkout Guard spots the attack, uses AI to tell it apart from a real
            sales rush, and shuts it down through PayPal within minutes, while the owner sleeps.
          </p>
          <div className="mt-8 flex flex-wrap gap-3">
            <Link
              href="/guard"
              className="inline-flex items-center gap-2 rounded-xl bg-guard px-5 py-3 font-medium text-white shadow-[0_12px_32px_-10px_rgb(76_141_255/0.8)] transition hover:bg-guard-dark"
            >
              Open the War Room <ArrowRight className="size-4" />
            </Link>
            <Link href="/shop" className="inline-flex items-center gap-2 rounded-xl bg-white/[0.06] px-5 py-3 font-medium text-ink-1 ring-1 ring-white/10 transition hover:bg-white/[0.1]">
              <Store className="size-4" /> Visit the demo shop
            </Link>
          </div>
          <p className="mt-4 text-xs text-ink-3">Judge login is in the README. The attacker is a clearly labelled simulation.</p>
        </div>

        <div className="relative">
          <div className="absolute -inset-6 -z-10 rounded-[2rem] bg-gradient-to-br from-guard/20 via-transparent to-emerald-400/10 blur-2xl" aria-hidden />
          <div className="panel p-5">
            <p className="flex items-center justify-between text-xs text-ink-3">
              <span className="font-semibold tracking-wider uppercase">One night at Wick &amp; Wax</span>
              <span>example</span>
            </p>
            <ol className="mt-4 space-y-3">
              {NIGHT.map((n, i) => (
                <li key={n.title} className="rise flex gap-3 rounded-xl bg-white/[0.025] p-3 ring-1 ring-white/[0.05]" style={{ animationDelay: `${i * 120}ms` }}>
                  <span className={`flex size-9 shrink-0 items-center justify-center rounded-lg ${n.tone}`}>
                    <n.icon className="size-4" />
                  </span>
                  <span className="min-w-0">
                    <span className="flex items-center gap-2 text-xs text-ink-3">
                      <span className="font-mono tabular-nums">{n.time}</span> · {n.title}
                    </span>
                    <span className="mt-0.5 block text-sm leading-snug text-ink-1">{n.text}</span>
                  </span>
                </li>
              ))}
            </ol>
            <div className="mt-4 flex items-start gap-3 rounded-2xl bg-white/[0.08] p-3.5 ring-1 ring-white/10 backdrop-blur">
              <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-guard">
                <BellRing className="size-4 text-white" />
              </span>
              <span>
                <span className="block text-[11px] font-semibold tracking-wide text-ink-3 uppercase">Checkout Guard · 07:30</span>
                <span className="block text-sm text-white">Attack stopped overnight. Test payments refunded. Nothing for you to do.</span>
              </span>
            </div>
          </div>
        </div>
      </section>

      {/* Problem */}
      <section className="border-y border-white/[0.06] bg-night-900/60">
        <div className="mx-auto grid max-w-6xl gap-6 px-6 py-14 md:grid-cols-3">
          {[
            { icon: CreditCard, title: "Each test looks like a normal order", text: "A $3 purchase with a valid card passes per-transaction fraud checks. The pattern only shows across hundreds of attempts." },
            { icon: CircleDollarSign, title: "The bill arrives later", text: "Every test charge that succeeds can come back as a dispute, with fees, and a high dispute rate can get a PayPal account limited." },
            { icon: Moon, title: "Nobody is awake to respond", text: "Small shops and nonprofits have no fraud team. They find out at breakfast, then spend the morning refunding by hand." },
          ].map((p) => (
            <div key={p.title}>
              <p.icon className="size-5 text-ink-3" />
              <h3 className="mt-3 font-semibold text-ink-1">{p.title}</h3>
              <p className="mt-1.5 text-sm leading-relaxed text-ink-2">{p.text}</p>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="mx-auto max-w-6xl scroll-mt-8 px-6 py-20">
        <p className="text-sm font-medium text-guard">How it works</p>
        <h2 className="mt-2 text-3xl font-semibold tracking-tight text-white sm:text-4xl">Four steps, one decision.</h2>
        <p className="mt-3 max-w-2xl text-ink-2">A cheap deterministic detector runs all the time. The AI is asked only when something looks wrong, and fixed rules have the final say on every action.</p>
        <ol className="mt-10 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {STEPS.map((s, i) => (
            <li key={s.name} className="panel relative p-5">
              <span className="absolute top-5 right-5 font-mono text-xs text-ink-3">0{i + 1}</span>
              <span className="flex size-10 items-center justify-center rounded-xl bg-white/[0.05] ring-1 ring-white/10">
                <s.icon className="size-5 text-ink-1" />
              </span>
              <h3 className="mt-4 text-lg font-semibold text-ink-1">{s.name}</h3>
              <p className="text-xs font-medium text-guard">{s.who}</p>
              <p className="mt-2 text-sm leading-relaxed text-ink-2">{s.text}</p>
            </li>
          ))}
        </ol>
        <p className="mt-6 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3 text-sm text-ink-2">
          <span className="text-violet-300">The AI advises</span> <ArrowRight className="size-3.5 text-ink-3" /> <span className="text-amber-300">fixed rules decide</span>{" "}
          <ArrowRight className="size-3.5 text-ink-3" /> <span className="text-sky-300">PayPal carries it out</span>
        </p>
      </section>

      {/* Attack vs rush */}
      <section className="mx-auto max-w-6xl px-6 pb-20">
        <h2 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">Attack, or your best sales night?</h2>
        <p className="mt-3 max-w-2xl text-ink-2">
          The hard part isn&apos;t spotting a spike. It&apos;s not punishing real customers. Checkout Guard reads the whole pattern before it acts.
        </p>
        <div className="mt-10 grid gap-4 md:grid-cols-2">
          <Compare
            icon={Bug}
            tone="red"
            title="Card-testing bot"
            signals={["Almost every attempt on the cheapest item", "A new card on nearly every attempt", "Banks decline them with fraud codes", "Email domains the shop has never seen"]}
            outcome="3-D Secure on, bot devices blocked, test charges refunded"
          />
          <Compare
            icon={PartyPopper}
            tone="sky"
            title="Genuine flash sale"
            signals={["Varied items and basket sizes", "Returning customers, everyday email providers", "Few declines, and honest ones: insufficient funds", "Often announced by the owner"]}
            outcome="Nobody blocked. Checkout Guard keeps watching."
          />
        </div>
      </section>

      {/* PayPal */}
      <section id="paypal" className="scroll-mt-8 border-y border-white/[0.06] bg-night-900/60">
        <div className="mx-auto max-w-6xl px-6 py-20">
          <p className="text-sm font-medium text-guard">Built on PayPal</p>
          <h2 className="mt-2 text-3xl font-semibold tracking-tight text-white sm:text-4xl">PayPal is the hands.</h2>
          <p className="mt-3 max-w-2xl text-ink-2">Every defence is a PayPal capability the merchant already has. Checkout Guard decides when to use it, and Gemini helps it decide.</p>
          <div className="mt-10 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {PAYPAL.map((p) => (
              <div key={p.name} className="flex gap-3 rounded-xl border border-white/[0.07] bg-white/[0.02] p-4">
                <p.icon className="mt-0.5 size-5 shrink-0 text-sky-300" />
                <div>
                  <p className="font-medium text-ink-1">{p.name}</p>
                  <p className="mt-0.5 text-sm text-ink-2">{p.text}</p>
                </div>
              </div>
            ))}
          </div>
          <p className="mt-4 flex items-center gap-2 text-sm text-ink-3">
            <Sparkles className="size-4 text-violet-300" /> AI: Google Gemini with JSON-schema structured output, a fallback model chain and a circuit breaker.
          </p>
        </div>
      </section>

      {/* Safety + transparency */}
      <section className="mx-auto grid max-w-6xl gap-4 px-6 py-20 md:grid-cols-2">
        <div className="panel p-6">
          <h3 className="flex items-center gap-2 font-semibold text-ink-1">
            <ShieldCheck className="size-5 text-emerald-300" /> The AI never moves money
          </h3>
          <ul className="mt-4 space-y-2.5 text-sm text-ink-2">
            {[
              "Refunds need fixed rules, the owner's caps and a payment matching the attack signature",
              "Anything above the owner's limits waits for approval; a kill switch turns actions off",
              "The model sees totals and masked rows only: email domain, hashed device, no card numbers",
              "Defences relax on their own when the attack stops",
            ].map((t) => (
              <li key={t} className="flex gap-2.5">
                <Check className="mt-0.5 size-4 shrink-0 text-emerald-300" /> {t}
              </li>
            ))}
          </ul>
        </div>
        <div className="panel p-6">
          <h3 className="flex items-center gap-2 font-semibold text-ink-1">
            <FlaskConical className="size-5 text-violet-300" /> What&apos;s real and what&apos;s simulated
          </h3>
          <dl className="mt-4 space-y-3 text-sm">
            <div className="flex gap-2.5">
              <Check className="mt-0.5 size-4 shrink-0 text-emerald-300" />
              <div>
                <dt className="font-medium text-ink-1">Real</dt>
                <dd className="text-ink-2">The PayPal sandbox integration used by the shop and by live-mode simulations (OAuth, Orders v2, refunds, webhook verification), the Gemini verdicts, the detector and the policy engine.</dd>
              </div>
            </div>
            <div className="flex gap-2.5">
              <Ban className="mt-0.5 size-4 shrink-0 text-violet-300" />
              <div>
                <dt className="font-medium text-ink-1">Simulated, and labelled SIM on screen</dt>
                <dd className="text-ink-2">The attacker, the &quot;stolen&quot; cards (sandbox test cards), the flash-sale crowd, and the seeded shop history.</dd>
              </div>
            </div>
            <div className="flex gap-2.5">
              <RefreshCcw className="mt-0.5 size-4 shrink-0 text-ink-3" />
              <div>
                <dt className="font-medium text-ink-1">Offline mode</dt>
                <dd className="text-ink-2">Simulations can synthesise PayPal&apos;s answer so the demo runs without sandbox card processing; those rows are marked in the data.</dd>
              </div>
            </div>
          </dl>
        </div>
      </section>

      {/* CTA */}
      <section className="mx-auto max-w-6xl px-6 pb-24">
        <div className="panel relative overflow-hidden px-8 py-12 text-center">
          <div className="absolute inset-0 -z-10 bg-[radial-gradient(60%_80%_at_50%_0%,rgb(76_141_255/0.18),transparent)]" aria-hidden />
          <h2 className="text-3xl font-semibold tracking-tight text-white">Watch it stop an attack.</h2>
          <p className="mx-auto mt-3 max-w-xl text-ink-2">Open the War Room and press &quot;Card-testing attack&quot;. Then try &quot;Flash sale&quot; and watch it leave real customers alone.</p>
          <div className="mt-7 flex flex-wrap justify-center gap-3">
            <Link href="/guard" className="inline-flex items-center gap-2 rounded-xl bg-guard px-5 py-3 font-medium text-white hover:bg-guard-dark">
              Open the War Room <ArrowRight className="size-4" />
            </Link>
            <Link href="/shop" className="inline-flex items-center gap-2 rounded-xl bg-white/[0.06] px-5 py-3 font-medium text-ink-1 ring-1 ring-white/10 hover:bg-white/[0.1]">
              Visit the demo shop
            </Link>
          </div>
        </div>
      </section>

      <footer className="border-t border-white/[0.06] py-8 text-center text-xs text-ink-3">
        Checkout Guard · built for the PayPal AI Hackathon 2026 · MIT licensed · PayPal sandbox only, no real payments
      </footer>
    </div>
  );
}

function Compare({ icon: Icon, tone, title, signals, outcome }: { icon: LucideIcon; tone: "red" | "sky"; title: string; signals: string[]; outcome: string }) {
  const c = tone === "red" ? { tile: "bg-red-400/10 text-red-300", ring: "ring-red-400/20", out: "bg-red-400/10 text-red-200" } : { tile: "bg-sky-400/10 text-sky-300", ring: "ring-sky-400/20", out: "bg-sky-400/10 text-sky-200" };
  return (
    <div className={`panel p-6 ring-1 ${c.ring}`}>
      <div className="flex items-center gap-3">
        <span className={`flex size-10 items-center justify-center rounded-xl ${c.tile}`}>
          <Icon className="size-5" />
        </span>
        <h3 className="text-lg font-semibold text-ink-1">{title}</h3>
      </div>
      <ul className="mt-5 space-y-2 text-sm text-ink-2">
        {signals.map((s) => (
          <li key={s} className="flex gap-2.5">
            <span className={`mt-1.5 size-1.5 shrink-0 rounded-full ${tone === "red" ? "bg-red-300" : "bg-sky-300"}`} /> {s}
          </li>
        ))}
      </ul>
      <p className={`mt-5 flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium ${c.out}`}>
        {tone === "red" ? <ShieldCheck className="size-4" /> : <Check className="size-4" />}
        {outcome}
      </p>
    </div>
  );
}
