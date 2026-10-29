import { ArrowLeft, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useId } from "react";

/** The frame shared by sign-in and sign-up: logo, title, the form, and a footer line. */
export function AuthCard({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  return (
    <main id="main" className="night relative flex min-h-screen flex-col items-center justify-center px-4 py-16">
      <Link href="/" className="absolute top-5 left-5 flex items-center gap-1.5 rounded-md px-2 py-1 text-sm text-ink-3 hover:text-ink-1">
        <ArrowLeft className="size-4" aria-hidden /> Home
      </Link>
      <div className="w-full max-w-sm">
        <div className="mb-6 flex flex-col items-center text-center">
          <span className="flex size-12 items-center justify-center rounded-xl bg-brand shadow-sm">
            <ShieldCheck className="size-6 text-white" aria-hidden />
          </span>
          <h1 className="mt-4 text-2xl font-semibold tracking-tight text-ink-1">{title}</h1>
          <p className="mt-1 text-sm text-ink-3">{subtitle}</p>
        </div>
        <div className="panel p-6 sm:p-8">{children}</div>
        <p className="mt-6 text-center text-sm text-ink-3">{footer}</p>
      </div>
    </main>
  );
}

export function Field({
  label,
  hint,
  ...input
}: { label: string; hint?: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  const id = useId();
  return (
    <div>
      <label htmlFor={id} className="text-sm font-medium text-ink-2">
        {label}
      </label>
      <input
        {...input}
        id={id}
        aria-describedby={hint ? `${id}-hint` : undefined}
        className="mt-1.5 block w-full rounded-lg border border-line-strong bg-white px-3 py-2.5 text-[15px] text-ink-1 placeholder:text-ink-3 focus:border-guard focus:ring-2 focus:ring-guard/20 focus:outline-none"
      />
      {hint && (
        <p id={`${id}-hint`} className="mt-1 text-xs text-ink-3">
          {hint}
        </p>
      )}
    </div>
  );
}

export function FormError({ children }: { children: React.ReactNode }) {
  return (
    <p role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      {children}
    </p>
  );
}
