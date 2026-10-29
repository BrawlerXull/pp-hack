import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import type { Tone } from "@/lib/format";

// Shared building blocks for the owner dashboard: white cards on a light grey canvas, one blue accent,
// status colour only where it means something. Text never goes below 12px (muted) or 14px (body).

export function Card({
  title,
  subtitle,
  icon: Icon,
  action,
  children,
  className = "",
  bodyClassName = "p-5",
  id,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  icon?: LucideIcon;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  id?: string;
}) {
  const headingId = id ? `${id}-title` : undefined;
  return (
    <section className={`panel min-w-0 ${className}`} aria-labelledby={headingId} id={id}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-3 border-b border-line px-5 py-3.5">
          <div className="flex min-w-0 items-center gap-2">
            {Icon && <Icon className="size-4 shrink-0 text-ink-3" aria-hidden />}
            <div className="min-w-0">
              <h2 id={headingId} className="text-[15px] font-semibold text-ink-1">
                {title}
              </h2>
              {subtitle && <p className="mt-0.5 text-[13px] text-ink-3">{subtitle}</p>}
            </div>
          </div>
          {action && <div className="shrink-0">{action}</div>}
        </header>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export const TONE_TEXT: Record<Tone, string> = {
  good: "text-emerald-700",
  bad: "text-red-700",
  warn: "text-amber-700",
  info: "text-blue-700",
  neutral: "text-ink-2",
};

const TONE: Record<Tone, string> = {
  good: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  bad: "bg-red-50 text-red-800 ring-red-200",
  warn: "bg-amber-50 text-amber-900 ring-amber-200",
  info: "bg-blue-50 text-blue-800 ring-blue-200",
  neutral: "bg-gray-100 text-ink-2 ring-line",
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
    primary: "bg-guard text-white hover:bg-guard-dark",
    secondary: "bg-white text-ink-1 ring-1 ring-inset ring-line-strong hover:bg-gray-50",
    danger: "bg-red-600 text-white hover:bg-red-700",
    ghost: "text-ink-2 hover:bg-gray-100 hover:text-ink-1",
  }[variant];
  const sizing = size === "sm" ? "h-8 px-3 text-[13px]" : "h-9 px-3.5 text-sm";
  return (
    <button
      type="button"
      {...props}
      className={`inline-flex items-center justify-center gap-1.5 rounded-lg font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${sizing} ${styles} ${className}`}
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
    <div className="panel px-5 py-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[13px] font-medium text-ink-2">{label}</p>
        {Icon && <Icon className="size-4 text-ink-3" aria-hidden />}
      </div>
      <p className={`mt-1.5 text-[28px] leading-9 font-semibold tracking-tight tabular-nums ${tone === "bad" || tone === "warn" ? TONE_TEXT[tone] : "text-ink-1"}`}>{value}</p>
      {hint && <p className="mt-0.5 text-[13px] text-ink-3">{hint}</p>}
    </div>
  );
}

export function Empty({ children, icon: Icon }: { children: ReactNode; icon?: LucideIcon }) {
  return (
    <div className="flex flex-col items-center gap-2 py-10 text-center text-sm text-ink-3">
      {Icon && <Icon className="size-6 text-ink-3" aria-hidden />}
      {children}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-xl bg-gray-100 ${className}`} aria-hidden />;
}

export function LiveDot({ tone = "good" }: { tone?: "good" | "bad" | "warn" | "neutral" }) {
  const color = { good: "bg-emerald-500", bad: "bg-red-500", warn: "bg-amber-500", neutral: "bg-gray-400" }[tone];
  return <span className={`inline-block size-2 shrink-0 rounded-full ${color} ${tone === "good" ? "live-dot" : ""}`} aria-hidden />;
}

export function PageHeader({ title, subtitle, action }: { title: ReactNode; subtitle?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight text-ink-1">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-ink-2">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

/** An on/off switch with its label (role=switch, so screen readers announce on/off). */
export function Toggle({
  label,
  hint,
  checked,
  onChange,
  danger,
  disabled,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-4">
      <span>
        <span className="block text-sm font-medium text-ink-1">{label}</span>
        {hint && <span className="mt-0.5 block text-xs leading-relaxed text-ink-3">{hint}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        onClick={() => onChange(!checked)}
        disabled={disabled}
        className={`relative h-6 w-11 shrink-0 rounded-full transition disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-guard ${
          checked ? (danger ? "bg-red-500" : "bg-guard") : "bg-gray-300"
        }`}
      >
        <span className={`absolute top-0.5 size-5 rounded-full bg-white shadow transition-all ${checked ? "left-5.5" : "left-0.5"}`} />
      </button>
    </div>
  );
}
