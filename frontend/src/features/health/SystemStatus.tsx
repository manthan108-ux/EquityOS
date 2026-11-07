"use client";

import { useEffect, useState } from "react";

import {
  API_URL,
  fetchLiveness,
  fetchReadiness,
  type LiveHealth,
  type ReadyHealth,
} from "@/services/api";

type State =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "done"; live: LiveHealth; ready: ReadyHealth };

function Row({ label, ok, value }: { label: string; ok: boolean; value: string }) {
  return (
    <div className="flex items-center justify-between py-3">
      <span className="text-sm text-zinc-500 dark:text-zinc-400">{label}</span>
      <span className="flex items-center gap-2 text-sm font-medium">
        <span
          aria-hidden
          className={`h-2 w-2 rounded-full ${ok ? "bg-emerald-500" : "bg-red-500"}`}
        />
        {value}
      </span>
    </div>
  );
}

export default function SystemStatus() {
  const [state, setState] = useState<State>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchLiveness(), fetchReadiness()])
      .then(([live, ready]) => !cancelled && setState({ phase: "done", live, ready }))
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            phase: "error",
            message: err instanceof Error ? err.message : "Unknown error",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section
      aria-label="System status"
      className="w-full max-w-md rounded-xl border border-zinc-200 bg-white px-6 py-4 dark:border-zinc-800 dark:bg-zinc-900"
    >
      <h2 className="pb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
        System status
      </h2>
      <div className="divide-y divide-zinc-100 dark:divide-zinc-800">
        {state.phase === "loading" && <p className="py-3 text-sm text-zinc-500">Checking…</p>}
        {state.phase === "error" && (
          <>
            <Row label="Backend API" ok={false} value="Unreachable" />
            <p className="py-3 text-xs text-zinc-500">
              {state.message}. Is the backend running at {API_URL}?
            </p>
          </>
        )}
        {state.phase === "done" && (
          <>
            <Row
              label="Backend API"
              ok={state.live.status === "ok"}
              value={`${state.live.service} v${state.live.version}`}
            />
            <Row
              label="Database"
              ok={state.ready.database === "up"}
              value={state.ready.database === "up" ? "Connected" : "Down"}
            />
          </>
        )}
      </div>
    </section>
  );
}
