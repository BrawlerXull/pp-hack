import { ArrowRight, Check, CreditCard, Moon, ShieldCheck, Store, TriangleAlert, Webhook, type LucideIcon } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: { absolute: "Checkout Guard · Stop card-testing attacks on your PayPal checkout" },
};

// One night at Wick & Wax, as the owner would read it the next morning (an example, not live data).
const NIGHT: { time: string; title: string; text: string }[] = [
  { time: "02:14", title: "Attack detected", text: "Hundreds of $3 orders on the cheapest item, a new card on almost every one." },
  { time: "02:14", title: "Judged: card testing, 99%", text: "The scorecard decides from the shop's own numbers. Gemini explains why in plain English." },
  { time: "02:15", title: "Defences on", text: "3-D Secure for first-time cards, 5 bot devices blocked, the targeted item put behind a human check." },
  { time: "02:20", title: "12 held payments voided", text: "Never charged, so no PayPal fee lost and nothing left to dispute." },
  { time: "07:30", title: "Morning report sent", text: "One calm message for the owner, and an evidence pack for PayPal support." },
];

const PROBLEM: { title: string; text: string }[] = [
  { title: "Each test looks like a normal order", text: "A $3 purchase with a valid card passes per-payment fraud checks. The pattern only shows across hundreds of attempts." },
  { title: "The bill arrives later", text: "Every test that succeeds can come back as a chargeback with a fee, and a high dispute rate can get the PayPal account limited." },
  { title: "Nobody is awake to respond", text: "Small shops have no fraud team. The owner finds out at breakfast and spends the morning refunding by hand." },
];

const STEPS: { name: string; who: string; text: string }[] = [
  { name: "Watch", who: "Detector", text: "Every checkout is checked before PayPal creates the order. Statistical triggers compare the last minutes with the shop's normal." },
  { name: "Decide", who: "Scorecard", text: "A fixed, explainable scorecard decides: card testing, stolen-account testing, a real rush or a PayPal outage. Gemini only breaks ties." },
  { name: "Act", who: "Policy rules", text: "Graduated defences inside the owner's limits: targeted 3-D Secure, device blocks, holds voided, refunds capped. They relax on their own." },
  { name: "Explain", who: "Gemini", text: "A plain-English alert, a morning report and an evidence pack. Every number comes from the shop's records, never from the AI." },
];

const CONNECT: { icon: LucideIcon; name: string; change: string; text: string }[] = [
  { icon: Webhook, name: "Webhooks only", change: "No code", text: "Point PayPal's webhooks at Checkout Guard. Attacks are detected and explained, and refunds are queued for one-tap approval." },
  { icon: ShieldCheck, name: "Guard API", change: "One call per order", text: "Call /v1/guard/check before creating an order. Adds blocking, 3-D Secure and payment holds." },
  { icon: Store, name: "Demo shop", change: "Built in", text: "Wick & Wax, a candle shop on the PayPal sandbox, with every defence switched on." },
];

const PAYPAL: { name: string; use: string }[] = [
  { name: "Orders v2", use: "Create, capture and authorise orders, each request idempotent" },
  { name: "Payments: authorizations", use: "Void an attack's held payments; capture everyone else's" },
  { name: "3-D Secure", use: "verification.method SCA_ALWAYS for first-time cards during an attack" },
  { name: "Payments: refunds", use: "Capped refunds for test charges that settled before detection" },
  { name: "Disputes API", use: "Accept a claim or provide evidence, after the owner decides" },
  { name: "Webhooks", use: "Every event signature-verified; the basis of webhooks-only mode" },
  { name: "JS SDK v6 + Card Fields", use: "The shop's PayPal button and PayPal-hosted card inputs" },
  { name: "FraudNet correlation", use: "PayPal-Client-Metadata-Id on every order, for PayPal's own risk engine" },
];

const CONTAINER = "mx-auto max-w-6xl px-5 sm:px-8";

export default function Landing() {
  return (
    <div className="min-h-screen bg-white text-ink-1">
      <a href="#main" className="skip-link">
        Skip to content
      </a>
      <header className="border-b border-line">
        <div className={`${CONTAINER} flex h-16 items-center justify-between`}>
          <Link href="/" className="flex items-center gap-2.5">
            <span className="flex size-8 items-center justify-center rounded-lg bg-brand">
              <ShieldCheck className="size-4.5 text-white" aria-hidden />
            </span>
            <span className="font-semibold">Checkout Guard</span>
          </Link>
          <nav className="flex items-center gap-1 text-sm" aria-label="Main">
            <a href="#how" className="hidden rounded-lg px-3 py-2 text-ink-2 hover:text-ink-1 md:inline">
              How it works
            </a>
            <a href="#paypal" className="hidden rounded-lg px-3 py-2 text-ink-2 hover:text-ink-1 md:inline">
              PayPal APIs
            </a>
            <Link href="/shop" className="rounded-lg px-3 py-2 text-ink-2 hover:text-ink-1">
              Demo shop
            </Link>
            <Link href="/guard" className="ml-1 inline-flex h-9 items-center rounded-lg bg-guard px-3.5 font-medium text-white hover:bg-guard-dark">
              Open the War Room
            </Link>
          </nav>
        </div>
      </header>

      <main id="main">
        <section className={`${CONTAINER} grid items-start gap-12 py-16 lg:grid-cols-[1.1fr_1fr] lg:py-20`}>
          <div>
            <p className="text-sm font-medium text-guard">For small shops on PayPal</p>
            <h1 className="mt-3 text-4xl leading-tight font-semibold tracking-tight sm:text-5xl">Stop card-testing attacks on your PayPal checkout.</h1>
            <p className="mt-5 max-w-xl text-lg leading-relaxed text-ink-2">
              Checkout Guard spots an attack within seconds, tells it apart from a real sales rush, and responds through PayPal while the owner sleeps:
              3-D Secure for first-time cards, bot devices blocked, and the attack&apos;s payments voided before they settle.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Link href="/guard" className="inline-flex h-11 items-center gap-2 rounded-lg bg-guard px-5 font-medium text-white hover:bg-guard-dark">
                Open the War Room <ArrowRight className="size-4" aria-hidden />
              </Link>
              <Link href="/shop" className="inline-flex h-11 items-center gap-2 rounded-lg px-5 font-medium text-ink-1 ring-1 ring-line-strong ring-inset hover:bg-gray-50">
                Try the demo shop
              </Link>
            </div>
            <p className="mt-4 text-sm text-ink-3">Runs on the PayPal sandbox. Simulated attacks are labelled as such. Judge login is in the README.</p>
          </div>

          <figure className="rounded-xl border border-line bg-white shadow-sm">
            <figcaption className="flex items-center justify-between border-b border-line px-5 py-3 text-sm">
              <span className="flex items-center gap-2 font-medium">
                <Moon className="size-4 text-ink-3" aria-hidden /> One night at Wick &amp; Wax
              </span>
              <span className="text-ink-3">Example</span>
            </figcaption>
            <ol className="divide-y divide-line">
              {NIGHT.map((n) => (
                <li key={n.title} className="grid grid-cols-[3.5rem_1fr] gap-3 px-5 py-3.5">
                  <time className="pt-0.5 font-mono text-[13px] text-ink-3">{n.time}</time>
                  <div>
                    <p className="text-sm font-medium">{n.title}</p>
                    <p className="mt-0.5 text-sm text-ink-2">{n.text}</p>
                  </div>
                </li>
              ))}
            </ol>
          </figure>
        </section>

        <section className="border-y border-line bg-gray-50" aria-labelledby="problem-title">
          <div className={`${CONTAINER} py-14`}>
            <h2 id="problem-title" className="text-2xl font-semibold tracking-tight">
              Why small shops get hit
            </h2>
            <div className="mt-8 grid gap-8 md:grid-cols-3">
              {PROBLEM.map((p) => (
                <div key={p.title}>
                  <h3 className="font-semibold">{p.title}</h3>
                  <p className="mt-2 text-[15px] leading-relaxed text-ink-2">{p.text}</p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section id="how" className={`${CONTAINER} scroll-mt-8 py-16`} aria-labelledby="how-title">
          <h2 id="how-title" className="text-2xl font-semibold tracking-tight">
            How it works
          </h2>
          <p className="mt-2 max-w-2xl text-[15px] text-ink-2">Fixed rules decide and act. The AI explains and breaks ties; it never moves money.</p>
          <ol className="mt-8 grid gap-6 md:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <li key={s.name} className="rounded-xl border border-line p-5">
                <p className="flex items-center gap-2 text-sm text-ink-3">
                  <span className="flex size-6 items-center justify-center rounded-full bg-gray-100 text-xs font-semibold text-ink-1">{i + 1}</span>
                  {s.who}
                </p>
                <h3 className="mt-3 text-lg font-semibold">{s.name}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-ink-2">{s.text}</p>
              </li>
            ))}
          </ol>
        </section>

        <section className="border-y border-line bg-gray-50" aria-labelledby="rush-title">
          <div className={`${CONTAINER} py-16`}>
            <h2 id="rush-title" className="text-2xl font-semibold tracking-tight">
              An attack, or your best sales night?
            </h2>
            <p className="mt-2 max-w-2xl text-[15px] text-ink-2">The hard part is not spotting a spike. It is not punishing real customers.</p>
            <div className="mt-8 grid gap-6 md:grid-cols-2">
              <Compare
                title="Card-testing bot"
                icon={TriangleAlert}
                points={["Almost every attempt on the cheapest item", "A new card on nearly every attempt", "Banks decline with fraud codes", "Email domains the shop has never seen"]}
                result="Defences on, the attack's payments voided"
                tone="bad"
              />
              <Compare
                title="Genuine flash sale"
                icon={Check}
                points={["Varied items and basket sizes", "Returning customers, everyday email providers", "Few declines, and honest ones", "Often announced by the owner"]}
                result="Nobody blocked. Returning customers skip every extra check"
                tone="good"
              />
            </div>
          </div>
        </section>

        <section className={`${CONTAINER} py-16`} aria-labelledby="connect-title">
          <h2 id="connect-title" className="text-2xl font-semibold tracking-tight">
            Works with the checkout a shop already has
          </h2>
          <div className="mt-8 grid gap-6 md:grid-cols-3">
            {CONNECT.map(({ icon: Icon, name, change, text }) => (
              <div key={name} className="rounded-xl border border-line p-5">
                <Icon className="size-5 text-guard" aria-hidden />
                <h3 className="mt-3 font-semibold">{name}</h3>
                <p className="text-sm text-ink-3">{change}</p>
                <p className="mt-2 text-sm leading-relaxed text-ink-2">{text}</p>
              </div>
            ))}
          </div>
        </section>

        <section id="paypal" className="scroll-mt-8 border-y border-line bg-gray-50" aria-labelledby="paypal-title">
          <div className={`${CONTAINER} py-16`}>
            <h2 id="paypal-title" className="text-2xl font-semibold tracking-tight">
              Built on PayPal
            </h2>
            <p className="mt-2 max-w-2xl text-[15px] text-ink-2">Every defence is a PayPal capability the merchant already has. Checkout Guard decides when to use it.</p>
            <div className="mt-8 overflow-hidden rounded-xl border border-line bg-white">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">PayPal APIs Checkout Guard calls</caption>
                <thead className="bg-gray-50 text-ink-2">
                  <tr>
                    <th scope="col" className="px-5 py-3 font-semibold">
                      PayPal API
                    </th>
                    <th scope="col" className="px-5 py-3 font-semibold">
                      Used for
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {PAYPAL.map((p) => (
                    <tr key={p.name}>
                      <th scope="row" className="px-5 py-3 font-medium whitespace-nowrap">
                        {p.name}
                      </th>
                      <td className="px-5 py-3 text-ink-2">{p.use}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <section className={`${CONTAINER} grid gap-6 py-16 md:grid-cols-2`} aria-label="Safety and honesty">
          <div className="rounded-xl border border-line p-6">
            <h2 className="text-lg font-semibold">The AI never moves money</h2>
            <ul className="mt-4 space-y-2.5 text-sm text-ink-2">
              {[
                "Refunds and voids need fixed rules, the owner's caps and a match to the attack",
                "Anything above the owner's limits waits for approval; a kill switch turns actions off",
                "The model sees totals and masked rows only: no card numbers, no names",
                "Defences relax on their own when the attack stops",
              ].map((t) => (
                <li key={t} className="flex gap-2">
                  <Check className="mt-0.5 size-4 shrink-0 text-emerald-700" aria-hidden /> {t}
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-xl border border-line p-6">
            <h2 className="text-lg font-semibold">What is real and what is simulated</h2>
            <dl className="mt-4 space-y-3 text-sm">
              <div>
                <dt className="font-medium">Real</dt>
                <dd className="text-ink-2">The PayPal sandbox integration (orders, 3-D Secure, holds, refunds, disputes, webhooks), the Gemini calls, the detector and the policy engine.</dd>
              </div>
              <div>
                <dt className="font-medium">Simulated, and labelled SIM on screen</dt>
                <dd className="text-ink-2">The attacker, the &quot;stolen&quot; sandbox test cards, the flash-sale crowd and the seeded shop history.</dd>
              </div>
            </dl>
          </div>
        </section>

        <section className="border-t border-line bg-gray-50">
          <div className={`${CONTAINER} flex flex-col items-start justify-between gap-6 py-14 md:flex-row md:items-center`}>
            <div>
              <h2 className="text-2xl font-semibold tracking-tight">Watch it stop an attack</h2>
              <p className="mt-2 text-[15px] text-ink-2">Open the War Room, run a card-testing attack from the sidebar, then a flash sale.</p>
            </div>
            <div className="flex flex-wrap gap-3">
              <Link href="/guard" className="inline-flex h-11 items-center gap-2 rounded-lg bg-guard px-5 font-medium text-white hover:bg-guard-dark">
                Open the War Room <ArrowRight className="size-4" aria-hidden />
              </Link>
              <Link href="/shop" className="inline-flex h-11 items-center gap-2 rounded-lg bg-white px-5 font-medium text-ink-1 ring-1 ring-line-strong ring-inset hover:bg-gray-50">
                <CreditCard className="size-4" aria-hidden /> Try the demo shop
              </Link>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-line">
        <p className={`${CONTAINER} py-6 text-sm text-ink-3`}>Checkout Guard · PayPal AI Hackathon 2026 · MIT licensed · PayPal sandbox only, no real payments</p>
      </footer>
    </div>
  );
}

function Compare({ title, icon: Icon, points, result, tone }: { title: string; icon: LucideIcon; points: string[]; result: string; tone: "bad" | "good" }) {
  return (
    <div className="rounded-xl border border-line bg-white p-6">
      <h3 className="flex items-center gap-2 text-lg font-semibold">
        <Icon className={`size-5 ${tone === "bad" ? "text-red-700" : "text-emerald-700"}`} aria-hidden /> {title}
      </h3>
      <ul className="mt-4 space-y-2 text-sm text-ink-2">
        {points.map((p) => (
          <li key={p} className="flex gap-2">
            <span className="mt-2 size-1.5 shrink-0 rounded-full bg-gray-400" aria-hidden />
            {p}
          </li>
        ))}
      </ul>
      <p className={`mt-5 rounded-lg px-3 py-2 text-sm font-medium ${tone === "bad" ? "bg-red-50 text-red-800" : "bg-emerald-50 text-emerald-800"}`}>{result}</p>
    </div>
  );
}
