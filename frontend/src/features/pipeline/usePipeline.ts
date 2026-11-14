"use client";

import { useCallback, useEffect, useState } from "react";

import { useApiKey } from "@/features/research/AccessKey";
import { explain, type UiError } from "@/features/research/useResearch";
import {
  fetchFailures,
  fetchHealth,
  fetchMetrics,
  fetchOverview,
  fetchRun,
  fetchRuns,
  isActive,
  type Failure,
  type Health,
  type Metrics,
  type Overview,
  type Run,
  type RunDetail,
} from "@/services/pipeline";

const POLL_MS = 4000;

/**
 * The persisted state of the pipeline: overview counts, recent runs, the selected run with every
 * company's stage states, and current failures. Refreshes itself while a run is active.
 */
export function usePipeline() {
  const key = useApiKey();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [failures, setFailures] = useState<Failure[]>([]);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!key) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    (async () => {
      try {
        const [ov, rs, fl] = await Promise.all([fetchOverview(), fetchRuns(), fetchFailures()]);
        const id = selected ?? ov.active_run?.id ?? ov.latest_run?.id ?? null;
        const d = id !== null ? await fetchRun(id) : null;
        // Monitoring is extra: if it cannot be read the rest of the page still works.
        const [h, m] = await Promise.all([
          fetchHealth().catch(() => null),
          fetchMetrics().catch(() => null),
        ]);
        if (!live) return;
        setOverview(ov);
        setRuns(rs);
        setFailures(fl);
        setDetail(d);
        setHealth(h);
        setMetrics(m);
        setError(null);
        if (ov.active_run || (d && isActive(d.status))) timer = setTimeout(refresh, POLL_MS);
      } catch (e) {
        if (live) setError(explain(e));
      }
    })();
    return () => {
      live = false;
      if (timer) clearTimeout(timer);
    };
  }, [key, tick, selected, refresh]);

  const loading = !!key && overview === null && error === null;
  return { key, overview, runs, failures, detail, health, metrics, selected, setSelected, error, loading, refresh };
}
