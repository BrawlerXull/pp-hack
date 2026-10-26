"use client";

import { Flame, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import { CheckoutSheet, ProductArt } from "@/components/shop/CheckoutSheet";
import { fetcher, type Product } from "@/lib/api";

export default function ShopPage() {
  const { data: products, error } = useSWR<Product[]>("/shop/products", fetcher);
  const { data: config, error: configError } = useSWR<{ paypal_client_id: string }>("/shop/config", fetcher);
  const [selected, setSelected] = useState<Product | null>(null);

  return (
    <div className="flex min-h-screen flex-col bg-cream text-soot">
      <header className="border-b border-wax">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
          <div className="flex items-center gap-2">
            <Flame className="size-6 text-ember" />
            <span className="text-xl font-semibold tracking-tight">Wick &amp; Wax</span>
          </div>
          <nav className="flex items-center gap-6 text-sm text-soot/70">
            <span className="hidden sm:inline">Hand-poured in small batches</span>
            <Link
              href="/guard"
              className="inline-flex items-center gap-1.5 rounded-full bg-soot px-3.5 py-1.5 text-xs font-medium text-cream hover:bg-soot/90"
            >
              <ShieldCheck className="size-3.5" /> Shop owner
            </Link>
          </nav>
        </div>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-12">
        <section className="max-w-2xl">
          <p className="text-sm font-medium uppercase tracking-widest text-ember">Autumn collection</p>
          <h1 className="mt-3 text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
            Slow evenings, soft light.
          </h1>
          <p className="mt-4 text-lg text-soot/70">
            Soy candles poured by hand in our tiny studio. Every order is packed by Meera herself.
          </p>
        </section>

        <section className="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          {error && <p className="text-red-700">Couldn&apos;t load the shop right now. Please try again in a moment.</p>}
          {!products &&
            !error &&
            [0, 1, 2].map((i) => <div key={i} className="h-96 animate-pulse rounded-2xl bg-wax/70" />)}
          {products?.map((p) => (
            <article key={p.sku} className="group overflow-hidden rounded-2xl bg-white ring-1 ring-wax transition hover:shadow-lg">
              <ProductArt sku={p.sku} className="h-56 w-full" />
              <div className="p-5">
                <div className="flex items-start justify-between gap-3">
                  <h2 className="font-semibold">{p.name}</h2>
                  <p className="font-semibold">${p.price}</p>
                </div>
                <p className="mt-1 text-sm text-soot/60">{p.description}</p>
                <button
                  onClick={() => setSelected(p)}
                  className="mt-5 w-full rounded-lg bg-ember py-2.5 text-sm font-medium text-white transition hover:bg-ember-dark"
                >
                  Buy now
                </button>
              </div>
            </article>
          ))}
        </section>
      </main>

      <footer className="border-t border-wax py-6 text-center text-xs text-soot/50">
        Demo store for the PayPal AI Hackathon. Runs on the PayPal sandbox; no real payments. Protected by Checkout Guard.
      </footer>

      {selected && (
        <CheckoutSheet product={selected} clientId={config?.paypal_client_id} configError={!!configError} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
