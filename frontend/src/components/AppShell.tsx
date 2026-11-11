"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Suspense, useState, type ReactNode } from "react";

import { useApiKey } from "@/features/research/AccessKey";
import { useHealth } from "@/features/health/useHealth";
import { cn } from "@/components/ui";

type Icon = (p: { className?: string }) => ReactNode;
const svg = (d: string): Icon =>
  function NavIcon({ className }) {
    return (
      <svg aria-hidden viewBox="0 0 24 24" className={cn("h-[18px] w-[18px]", className)} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
        <path d={d} />
      </svg>
    );
  };

export const NAV: { href: string; label: string; icon: Icon; match: (p: string) => boolean }[] = [
  { href: "/", label: "Overview", icon: svg("M3 12 12 4l9 8M5 10v10h14V10"), match: (p) => p === "/" },
  { href: "/companies", label: "Companies", icon: svg("M4 20V6l8-3 8 3v14M9 20v-5h6v5M8 9h2m4 0h2"), match: (p) => p.startsWith("/companies") },
  { href: "/research", label: "Research Workspace", icon: svg("M4 5h16M4 12h10M4 19h16"), match: (p) => p.startsWith("/research") },
  { href: "/readiness", label: "Research Readiness", icon: svg("M5 13l4 4L19 7"), match: (p) => p.startsWith("/readiness") },
  { href: "/documents", label: "Documents", icon: svg("M7 3h7l5 5v13H7zM14 3v5h5"), match: (p) => p.startsWith("/documents") },
  { href: "/reports", label: "Reports", icon: svg("M5 20V10m5 10V4m5 16v-7m5 7V8"), match: (p) => p.startsWith("/reports") },
  { href: "/settings", label: "Settings", icon: svg("M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19 12l2-1-2-3-2 .5-1.5-1L15 5h-4l-.5 2.5-1.5 1L7 8l-2 3 2 1v2l-2 1 2 3 2-.5 1.5 1L11 19h4l.5-2.5 1.5-1 2 .5 2-3-2-1z"), match: (p) => p.startsWith("/settings") },
];

function Brand() {
  return (
    <Link href="/" className="flex items-center gap-2.5 px-2" aria-label="EquityOS home">
      <span aria-hidden className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand text-sm font-bold text-white">E</span>
      <span className="text-[17px] font-semibold tracking-tight text-ink">EquityOS</span>
    </Link>
  );
}

function NavList({ onNavigate }: { onNavigate?: () => void }) {
  const path = usePathname();
  return (
    <nav aria-label="Primary" className="mt-6 flex flex-col gap-0.5">
      {NAV.map(({ href, label, icon: I, match }) => {
        const on = match(path);
        return (
          <Link
            key={href}
            href={href}
            onClick={onNavigate}
            aria-current={on ? "page" : undefined}
            className={cn(
              "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
              on ? "bg-brand-soft text-brand" : "text-muted hover:bg-surface-2 hover:text-ink",
            )}
          >
            <I className={on ? "text-brand" : "text-faint"} />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

function StatusDot() {
  const h = useHealth();
  const tone = h.phase === "loading" ? "bg-faint" : h.phase === "ok" ? "bg-ok" : "bg-bad";
  const text = h.phase === "loading" ? "Checking system" : h.phase === "ok" ? "System operational" : h.phase === "degraded" ? "Database unavailable" : "Backend unreachable";
  return (
    <span className="flex items-center gap-2 text-xs text-muted" role="status">
      <span aria-hidden className={cn("h-2 w-2 rounded-full", tone)} />
      {text}
    </span>
  );
}

function KeyState() {
  const key = useApiKey();
  return (
    <Link
      href="/settings"
      className={cn(
        "rounded-full border px-2.5 py-1 text-xs font-medium",
        key ? "border-ok-line bg-ok-soft text-ok" : "border-warn-line bg-warn-soft text-warn",
      )}
    >
      {key ? "Access key set" : "Access key needed"}
    </Link>
  );
}

export default function AppShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[248px_1fr]">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2">
        Skip to content
      </a>
      {/* desktop sidebar */}
      <aside className="sticky top-0 hidden h-screen flex-col border-r border-line bg-surface px-4 py-5 lg:flex">
        <Brand />
        <Suspense fallback={null}>
          <NavList />
        </Suspense>
        <div className="mt-auto space-y-3 border-t border-line pt-4">
          <StatusDot />
          <p className="text-[11px] leading-4 text-faint">
            Research assistance, not investment advice or a promise of returns.
          </p>
        </div>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-3 border-b border-line bg-surface/95 px-4 backdrop-blur lg:px-8">
          <div className="flex items-center gap-3 lg:hidden">
            <button
              type="button"
              aria-label={open ? "Close navigation" : "Open navigation"}
              aria-expanded={open}
              aria-controls="mobile-nav"
              onClick={() => setOpen((v) => !v)}
              className="flex h-9 w-9 items-center justify-center rounded-lg border border-line-strong text-ink hover:bg-surface-2"
            >
              <svg aria-hidden viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
                <path d="M4 7h16M4 12h16M4 17h16" />
              </svg>
            </button>
            <Link href="/" className="text-base font-semibold text-ink">EquityOS</Link>
          </div>
          <div className="hidden text-sm text-muted lg:block">Evidence-driven equity research · NSE and BSE</div>
          <div className="flex items-center gap-3">
            <span className="hidden sm:block lg:hidden"><StatusDot /></span>
            <KeyState />
          </div>
        </header>

        {open && (
          <div id="mobile-nav" className="border-b border-line bg-surface px-4 pb-4 lg:hidden">
            <Suspense fallback={null}>
              <NavList onNavigate={() => setOpen(false)} />
            </Suspense>
            <div className="mt-3"><StatusDot /></div>
          </div>
        )}

        <main id="main" className="mx-auto w-full max-w-[1280px] flex-1 px-4 py-6 lg:px-8 lg:py-8">
          {children}
        </main>
      </div>
    </div>
  );
}
