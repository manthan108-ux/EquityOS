"use client";

// EquityOS design-system primitives. Light theme only; every colour comes from the tokens in globals.css.

import Link from "next/link";
import {
  useId,
  useRef,
  type ButtonHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
} from "react";

export const cn = (...parts: (string | false | null | undefined)[]) =>
  parts.filter(Boolean).join(" ");

// ---- buttons ---------------------------------------------------------------------------------

type Variant = "primary" | "secondary" | "tertiary" | "danger";
type Size = "sm" | "md";

const VARIANT: Record<Variant, string> = {
  primary:
    "bg-brand text-white border border-brand hover:bg-brand-hover hover:border-brand-hover disabled:bg-line disabled:border-line disabled:text-faint",
  secondary:
    "bg-surface text-ink border border-line-strong hover:bg-surface-2 hover:border-faint disabled:text-faint disabled:bg-surface-2",
  tertiary:
    "bg-transparent text-brand border border-transparent hover:bg-brand-soft disabled:text-faint",
  danger:
    "bg-surface text-bad border border-bad-line hover:bg-bad-soft disabled:text-faint disabled:border-line",
};
const SIZE: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px]",
  md: "h-10 px-4 text-sm",
};
const BASE =
  "inline-flex items-center justify-center gap-2 rounded-[8px] font-medium transition-colors select-none disabled:cursor-not-allowed";

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  /** Why the action is unavailable; shown on hover and announced to screen readers. */
  disabledReason?: string;
};

export function Button({
  variant = "secondary",
  size = "md",
  loading = false,
  disabledReason,
  disabled,
  className,
  children,
  type = "button",
  ...rest
}: ButtonProps) {
  const reasonId = useId();
  const off = disabled || loading || !!disabledReason;
  return (
    <>
      <button
        type={type}
        disabled={off}
        title={disabledReason}
        aria-describedby={disabledReason ? reasonId : undefined}
        aria-busy={loading || undefined}
        className={cn(BASE, VARIANT[variant], SIZE[size], className)}
        {...rest}
      >
        {loading && <Spinner />}
        {children}
      </button>
      {disabledReason && (
        <span id={reasonId} className="sr-only">
          {disabledReason}
        </span>
      )}
    </>
  );
}

export function ButtonLink({
  href,
  variant = "secondary",
  size = "md",
  className,
  children,
}: {
  href: string;
  variant?: Variant;
  size?: Size;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={href} className={cn(BASE, VARIANT[variant], SIZE[size], className)}>
      {children}
    </Link>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 24 24"
      className={cn("h-4 w-4 animate-spin", className)}
      fill="none"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

// ---- badges and status -----------------------------------------------------------------------

export type Tone = "ok" | "warn" | "bad" | "info" | "brand" | "neutral";
const TONE: Record<Tone, string> = {
  ok: "bg-ok-soft text-ok border-ok-line",
  warn: "bg-warn-soft text-warn border-warn-line",
  bad: "bg-bad-soft text-bad border-bad-line",
  info: "bg-info-soft text-info border-info-line",
  brand: "bg-brand-soft text-brand border-brand-line",
  neutral: "bg-surface-2 text-muted border-line",
};
const DOT: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  brand: "bg-brand",
  neutral: "bg-faint",
};

export function Badge({
  tone = "neutral",
  children,
  dot = false,
  className,
}: {
  tone?: Tone;
  children: ReactNode;
  dot?: boolean;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap",
        TONE[tone],
        className,
      )}
    >
      {dot && <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", DOT[tone])} />}
      {children}
    </span>
  );
}

/** A status whose meaning is carried by its words as well as its colour. */
export function StatusBadge({ tone, label }: { tone: Tone; label: string }) {
  return (
    <Badge tone={tone} dot>
      {label}
    </Badge>
  );
}

// ---- containers ------------------------------------------------------------------------------

export function Card({
  children,
  className,
  as: Tag = "section",
  ...rest
}: {
  children: ReactNode;
  className?: string;
  as?: "section" | "div" | "article";
} & React.HTMLAttributes<HTMLElement>) {
  return (
    <Tag
      className={cn("rounded-[12px] border border-line bg-surface shadow-[var(--shadow-card)]", className)}
      {...rest}
    >
      {children}
    </Tag>
  );
}

export function CardHeader({
  title,
  subtitle,
  actions,
  id,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  id?: string;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
      <div className="min-w-0">
        <h2 id={id} className="text-[15px] font-semibold text-ink">
          {title}
        </h2>
        {subtitle && <p className="mt-0.5 text-[13px] text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">{title}</h1>
        {subtitle && <p className="mt-1 max-w-3xl text-sm text-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
  tip,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  tip?: string;
}) {
  return (
    <div className="min-w-0">
      <dt className="flex items-center gap-1 text-xs font-medium tracking-wide text-muted uppercase">
        {label}
        {tip && <InfoTip text={tip} />}
      </dt>
      <dd
        className={cn(
          "tnum mt-1 truncate text-xl font-semibold",
          tone === "ok" && "text-ok",
          tone === "warn" && "text-warn",
          tone === "bad" && "text-bad",
          !tone && "text-ink",
        )}
      >
        {value}
      </dd>
      {hint && <p className="mt-0.5 text-xs text-muted">{hint}</p>}
    </div>
  );
}

// ---- feedback --------------------------------------------------------------------------------

export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("eq-skeleton h-4 w-full", className)} />;
}

export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <Card className="space-y-3 p-5" aria-busy="true" aria-label="Loading">
      <Skeleton className="h-5 w-1/3" />
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className={i === lines - 1 ? "w-2/3" : "w-full"} />
      ))}
    </Card>
  );
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center rounded-[12px] border border-dashed border-line-strong bg-surface-2 px-6 py-10 text-center">
      <p className="text-sm font-semibold text-ink">{title}</p>
      {children && <div className="mt-1 max-w-md text-[13px] text-muted">{children}</div>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  detail,
  onRetry,
}: {
  title?: string;
  message: string;
  /** Technical detail, kept out of the way but available. */
  detail?: string | null;
  onRetry?: () => void;
}) {
  return (
    <div role="alert" className="rounded-[12px] border border-bad-line bg-bad-soft p-4">
      <p className="text-sm font-semibold text-bad">{title}</p>
      <p className="mt-1 text-[13px] text-ink">{message}</p>
      {detail && (
        <details className="mt-2 text-xs text-muted">
          <summary className="cursor-pointer font-medium">Technical details</summary>
          <p className="mt-1 break-words font-mono">{detail}</p>
        </details>
      )}
      {onRetry && (
        <Button className="mt-3" size="sm" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
}: {
  tone?: Tone;
  title?: string;
  children: ReactNode;
}) {
  return (
    <div
      role={tone === "bad" ? "alert" : "status"}
      className={cn("rounded-[10px] border px-4 py-3 text-[13px]", TONE[tone])}
    >
      {title && <p className="font-semibold">{title}</p>}
      <div className={title ? "mt-0.5 text-ink" : "text-ink"}>{children}</div>
    </div>
  );
}

export function InfoTip({ text }: { text: string }) {
  const id = useId();
  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        aria-label={text}
        aria-describedby={id}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-line-strong text-[10px] font-semibold text-muted hover:border-brand hover:text-brand"
      >
        i
      </button>
      <span
        id={id}
        role="tooltip"
        className="pointer-events-none absolute top-5 left-1/2 z-30 hidden w-60 -translate-x-1/2 rounded-md border border-line bg-surface p-2 text-xs font-normal normal-case tracking-normal text-ink shadow-[var(--shadow-pop)] group-focus-within:block group-hover:block"
      >
        {text}
      </span>
    </span>
  );
}

export function ProgressBar({
  value,
  tone,
  label,
}: {
  value: number | null;
  tone?: Tone;
  label: string;
}) {
  const v = value === null ? 0 : Math.max(0, Math.min(100, value));
  const t: Tone = tone ?? (value === null ? "neutral" : v >= 75 ? "ok" : v >= 50 ? "warn" : "bad");
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value === null ? undefined : Math.round(v)}
      className="h-2 w-full overflow-hidden rounded-full bg-line"
    >
      <div className={cn("h-full rounded-full", DOT[t])} style={{ width: `${v}%` }} />
    </div>
  );
}

// ---- tabs ------------------------------------------------------------------------------------

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { id: T; label: string; badge?: ReactNode }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const move = (e: KeyboardEvent, i: number) => {
    const n = tabs.length;
    const next =
      e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : null;
    if (next === null) return;
    e.preventDefault();
    onChange(tabs[next].id);
    refs.current[tabs[next].id]?.focus();
  };
  return (
    <div
      role="tablist"
      aria-label={label}
      className="flex gap-1 overflow-x-auto border-b border-line"
    >
      {tabs.map((t, i) => {
        const on = t.id === value;
        return (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[t.id] = el;
            }}
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={on}
            aria-controls={`panel-${t.id}`}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(t.id)}
            onKeyDown={(e) => move(e, i)}
            className={cn(
              "-mb-px inline-flex items-center gap-2 border-b-2 px-3.5 py-2.5 text-sm font-medium whitespace-nowrap transition-colors",
              on
                ? "border-brand text-brand"
                : "border-transparent text-muted hover:border-line-strong hover:text-ink",
            )}
          >
            {t.label}
            {t.badge}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ id, children }: { id: string; children: ReactNode }) {
  return (
    <div role="tabpanel" id={`panel-${id}`} aria-labelledby={`tab-${id}`} className="pt-6">
      {children}
    </div>
  );
}

// ---- small helpers ---------------------------------------------------------------------------

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-xs font-medium tracking-wide text-muted uppercase">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-muted">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "h-10 w-full rounded-[8px] border border-line-strong bg-surface px-3 text-sm text-ink placeholder:text-faint hover:border-faint focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/15 disabled:bg-surface-2 disabled:text-faint";
