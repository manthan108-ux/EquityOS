"use client";

import { useEffect, useState } from "react";

import { useApiKey } from "@/features/research/AccessKey";
import { explain, type UiError } from "@/features/research/useResearch";
import {
  fetchAllReports,
  fetchOverview,
  listSessions,
  type Overview,
  type ReportListRow,
  type SessionRow,
} from "@/services/research";

type Loaded<T> = { data: T | null; error: UiError | null; loading: boolean; reload: () => void };

function useLoaded<T>(load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const key = useApiKey();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<UiError | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!key) return;
    let live = true;
    load()
      .then((d) => {
        if (!live) return;
        setData(d);
        setError(null);
      })
      .catch((e) => live && setError(explain(e)));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick, ...deps]);
  const loading = !!key && data === null && error === null;
  return { data, error, loading, reload: () => setTick((t) => t + 1) };
}

export const useOverview = () => useLoaded<Overview>(fetchOverview, []);
export const useSessions = () => useLoaded<SessionRow[]>(() => listSessions(), []);
export const useAllReports = () => useLoaded<ReportListRow[]>(() => fetchAllReports(), []);
