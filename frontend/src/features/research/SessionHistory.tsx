"use client";

import { useEffect, useState } from "react";

import { useApiKey } from "@/features/research/AccessKey";
import ResearchWorkspace from "@/features/research/ResearchWorkspace";
import { listSessions, type SessionRow } from "@/services/research";

const LABEL: Record<string, string> = {
  COMPLETED: "report ready",
  AWAITING_DOCUMENTS: "saved: waiting for documents",
  RUNNING: "running",
  READY: "ready",
  FAILED: "stopped: needs retry",
  CREATED: "starting",
};

export default function SessionHistory() {
  const key = useApiKey();
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [open, setOpen] = useState<SessionRow | null>(null);

  useEffect(() => {
    if (!key) return;
    let live = true;
    listSessions()
      .then((r) => live && setRows(r))
      .catch(() => live && setRows(null));
    return () => {
      live = false;
    };
  }, [key]);

  if (!key || !rows || rows.length === 0) return null;
  return (
    <section aria-label="Research history" className="w-full max-w-3xl">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500">
        Your research sessions
      </h2>
      <ul className="mt-2 divide-y divide-zinc-200 rounded-xl border border-zinc-200 bg-white dark:divide-zinc-800 dark:border-zinc-800 dark:bg-zinc-900">
        {rows.map((s) => (
          <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-2 text-sm">
            <span className="font-medium">{s.company ?? "Company"}</span>
            <span className="text-xs text-zinc-500">
              {LABEL[s.status] ?? s.status.toLowerCase()}
              {s.readiness_score !== null ? ` · readiness ${s.readiness_score}` : ""}
              {s.latest_report_version ? ` · report v${s.latest_report_version}` : ""}
              {s.updated_at ? ` · ${s.updated_at.slice(0, 10)}` : ""}
            </span>
            <button
              type="button"
              className="rounded border px-2 py-1 text-xs"
              onClick={() => setOpen(open?.id === s.id ? null : s)}
            >
              {open?.id === s.id ? "Close" : s.status === "COMPLETED" ? "Open" : "Resume"}
            </button>
          </li>
        ))}
      </ul>
      {open && (
        <ResearchWorkspace
          key={open.id}
          companyId={open.company_id}
          companyName={open.company ?? "Company"}
          sessionId={open.id}
        />
      )}
    </section>
  );
}
