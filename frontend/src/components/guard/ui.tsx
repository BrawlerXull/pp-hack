import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { Tone } from "@/lib/format";

export function Card({
  title,
  subtitle,
  icon: Icon,
  action,
  children,
  className = "",
  bodyClassName = "p-5",
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  icon?: LucideIcon;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`panel min-w-0 ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 px-5 pt-4 pb-3">
          <div className="flex min-w-0 items-start gap-2.5">
            {Icon && <Icon className="mt-0.5 size-4 shrink-0 text-ink-3" aria-hidden />}
            <div className="min-w-0">
              <h2 className="text-sm font-semibold text-ink-1">{title}</h2>
              {subtitle && <p className="mt-0.5 text-xs text-ink-3">{subtitle}</p>}
            </div>
          </div>
          {action}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export const TONE_TEXT: Record<Tone, string> = {
  good: "text-emerald-300",
  bad: "text-red-300",
  warn: "text-amber-300",
  info: "text-sky-300",
  neutral: "text-ink-2",
};

const TONE: Record<Tone, string> = {
  good: "bg-emerald-400/10 text-emerald-300 ring-emerald-400/25",
  bad: "bg-red-400/10 text-red-300 ring-red-400/25",
  warn: "bg-amber-400/10 text-amber-200 ring-amber-400/25",
  info: "bg-sky-400/10 text-sky-300 ring-sky-400/25",
  neutral: "bg-white/[0.05] text-ink-2 ring-white/10",
};

export function Badge({ tone = "neutral", children, className = "" }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset ${TONE[tone]} ${className}`}>
      {children}
    </span>
  );
}

export function Button({
  children,
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger" | "ghost"; size?: "sm" | "md" }) {
  const styles = {
    primary: "bg-guard text-white shadow-[0_8px_24px_-8px_rgb(76_141_255/0.6)] hover:bg-guard-dark",
    secondary: "bg-white/[0.06] text-ink-1 ring-1 ring-inset ring-white/10 hover:bg-white/[0.1]",
    danger: "bg-red-500 text-white shadow-[0_8px_24px_-8px_rgb(239_68_68/0.6)] hover:bg-red-600",
    ghost: "text-ink-2 hover:bg-white/[0.06] hover:text-ink-1",
  }[variant];
  const sizing = size === "sm" ? "px-2.5 py-1.5 text-xs" : "px-3.5 py-2 text-sm";
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-guard disabled:cursor-not-allowed disabled:opacity-50 ${sizing} ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
  icon: Icon,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  icon?: LucideIcon;
}) {
  return (
    <div className="panel relative overflow-hidden px-5 py-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-medium text-ink-3">{label}</p>
        {Icon && <Icon className={`size-4 ${tone ? TONE_TEXT[tone] : "text-ink-3"}`} aria-hidden />}
      </div>
      <p className={`mt-2 text-3xl font-semibold tracking-tight ${tone && tone !== "neutral" ? TONE_TEXT[tone] : "text-ink-1"}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-ink-3">{hint}</p>}
    </div>
  );
}

export function Empty({ children, icon: Icon }: { children: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-ink-3">
      {Icon && <Icon className="size-6 text-ink-3/70" aria-hidden />}
      {children}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-2xl bg-white/[0.04] ${className}`} />;
}

export function LiveDot({ tone = "good" }: { tone?: "good" | "bad" | "warn" | "neutral" }) {
  const color = { good: "bg-emerald-400", bad: "bg-red-400", warn: "bg-amber-400", neutral: "bg-ink-3" }[tone];
  return <span className={`inline-block size-2 rounded-full ${color} ${tone === "neutral" ? "" : "live-dot"}`} aria-hidden />;
}

export function PageHeader({ title, subtitle, action }: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink-1">{title}</h1>
        {subtitle && <p className="mt-1 max-w-2xl text-sm text-ink-2">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}
