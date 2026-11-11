"use client";

import { useEffect, useState } from "react";

import { fetchLiveness, fetchReadiness, type LiveHealth, type ReadyHealth } from "@/services/api";

export type Health =
  | { phase: "loading" }
  | { phase: "error"; message: string }
  | { phase: "degraded"; live: LiveHealth; ready: ReadyHealth }
  | { phase: "ok"; live: LiveHealth; ready: ReadyHealth };

/** Backend and database state, checked once per mount. */
export function useHealth(): Health {
  const [state, setState] = useState<Health>({ phase: "loading" });
  useEffect(() => {
    let live = true;
    Promise.all([fetchLiveness(), fetchReadiness()])
      .then(([l, r]) => {
        if (!live) return;
        setState(l.status === "ok" && r.database === "up" ? { phase: "ok", live: l, ready: r } : { phase: "degraded", live: l, ready: r });
      })
      .catch((e: unknown) => live && setState({ phase: "error", message: e instanceof Error ? e.message : "Unknown error" }));
    return () => {
      live = false;
    };
  }, []);
  return state;
}
