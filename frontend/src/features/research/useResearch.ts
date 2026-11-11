"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useApiKey } from "@/features/research/AccessKey";
import {
  ApiError,
  continueWithAvailable,
  fetchMissing,
  fetchReport,
  fetchReports,
  fetchResearchReadiness,
  fetchSession,
  fetchUploads,
  generateReport,
  listSessions,
  resumeResearch,
  startResearch,
  updatePreferences,
  uploadDocuments,
  type MissingAssessment,
  type Preferences,
  type Readiness,
  type Report,
  type ReportRow,
  type SessionRow,
  type SessionStatus,
  type Upload,
} from "@/services/research";

export type Bundle = {
  readiness: Readiness;
  missing: MissingAssessment;
  uploads: Upload[];
  reports: ReportRow[];
};

export type UiError = { message: string; detail: string | null };

/** A plain-language message for the user; the technical text is kept separately. */
export function explain(e: unknown): UiError {
  if (e instanceof ApiError) {
    const detail = `HTTP ${e.status}: ${e.message}`;
    if (e.status === 401) return { message: "The research access key was not accepted. Check it in Settings.", detail };
    if (e.status === 503) return { message: "Research access is not configured on the server (RESEARCH_API_KEYS).", detail };
    if (e.status === 429) return { message: "Too many requests. Wait a moment and try again.", detail };
    if (e.status === 413) return { message: "That file is larger than the upload limit.", detail };
    if (e.status === 409) return { message: "That action is not allowed yet. See the reasons shown for the report or step.", detail };
    if (e.status === 404) return { message: "That item was not found. It may have been removed.", detail };
    if (e.status >= 500) return { message: "The server could not complete that request. It is safe to try again.", detail };
    return { message: e.message, detail };
  }
  return { message: "Could not reach the server. Is the backend running?", detail: e instanceof Error ? e.message : null };
}

const loadAll = async (id: string): Promise<Bundle> => {
  const [readiness, missing, uploads, reports] = await Promise.all([
    fetchResearchReadiness(id),
    fetchMissing(id),
    fetchUploads(id),
    fetchReports(id),
  ]);
  return { readiness, missing, uploads, reports };
};

/**
 * Everything the company workspace needs from the research API: the company's sessions, the open
 * session's progress, readiness, missing documents, uploads and reports, plus the actions on them.
 */
export function useResearch(companyId: string, preferredSession?: string | null) {
  const key = useApiKey();
  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [data, setData] = useState<Bundle | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<UiError | null>(null);
  const picked = useRef<string | null>(null);

  const refreshSessions = useCallback(
    () =>
      listSessions(false, companyId)
        .then(setSessions)
        .catch((e) => setError(explain(e))),
    [companyId],
  );

  // The company's saved sessions; the chosen one (or the newest) opens automatically.
  useEffect(() => {
    if (!key) return;
    let live = true;
    listSessions(false, companyId)
      .then(async (rows) => {
        if (!live) return;
        setSessions(rows);
        const want = preferredSession && rows.find((r) => r.id === preferredSession) ? preferredSession : rows[0]?.id;
        if (want && picked.current !== want) {
          picked.current = want;
          setSession(await fetchSession(want));
        }
      })
      .catch((e) => live && setError(explain(e)));
    return () => {
      live = false;
    };
  }, [key, companyId, preferredSession]);
  const loading = !!key && sessions === null && error === null;

  const sid = session?.id;
  const running = session?.status === "RUNNING" || session?.status === "CREATED";

  // While the research runs, poll its progress.
  useEffect(() => {
    if (!sid || !running) return;
    const t = setTimeout(() => {
      fetchSession(sid).then(setSession).catch((e) => setError(explain(e)));
    }, 1500);
    return () => clearTimeout(t);
  }, [sid, running, session]);

  // When it settles (or changes), load readiness, missing documents, uploads and reports.
  const settled = sid && !running ? `${sid}:${session?.updated_at}` : null;
  useEffect(() => {
    if (!settled || !sid) return;
    let live = true;
    loadAll(sid)
      .then(async (b) => {
        if (!live) return;
        setData(b);
        if (b.reports.length) {
          const latest = await fetchReport(b.reports[0].id);
          if (live) setReport(latest);
        } else setReport(null);
      })
      .catch((e) => live && setError(explain(e)));
    return () => {
      live = false;
    };
  }, [settled, sid]);

  // While uploads are processed in the background, poll them.
  const processing = data?.uploads.some((u) => u.status === "RECEIVED" || u.status === "PROCESSING");
  useEffect(() => {
    if (!sid || !processing) return;
    const t = setTimeout(() => {
      fetchSession(sid).then(setSession).catch((e) => setError(explain(e)));
      fetchUploads(sid)
        .then((uploads) => setData((d) => (d ? { ...d, uploads } : d)))
        .catch((e) => setError(explain(e)));
    }, 2000);
    return () => clearTimeout(t);
  }, [sid, processing, data]);

  const run = useCallback(
    async <T,>(fn: () => Promise<T>, after?: (r: T) => void) => {
      setBusy(true);
      setError(null);
      try {
        const r = await fn();
        after?.(r);
        return r;
      } catch (e) {
        setError(explain(e));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const adopt = (s: SessionStatus) => {
    picked.current = s.id;
    setSession(s);
    refreshSessions();
  };

  return {
    key,
    sessions,
    session,
    data,
    report,
    busy,
    loading,
    running,
    error,
    clearError: () => setError(null),
    start: (prefs?: Preferences) => run(() => startResearch(companyId, prefs), adopt),
    resume: () => session && run(() => resumeResearch(session.id), adopt),
    continueAvailable: () => session && run(() => continueWithAvailable(session.id), adopt),
    savePreferences: (p: Preferences) => session && run(() => updatePreferences(session.id, p), adopt),
    generate: (mode: Report["mode"]) =>
      session &&
      run(
        () => generateReport(session.id, mode),
        (r) => {
          setReport(r);
          fetchReports(session.id).then((reports) => setData((d) => (d ? { ...d, reports } : d)));
          fetchSession(session.id).then(setSession);
        },
      ),
    openReport: (id: string) => run(() => fetchReport(id), setReport),
    openSession: (id: string) =>
      run(() => fetchSession(id), (s) => {
        picked.current = s.id;
        setData(null);
        setReport(null);
        setSession(s);
      }),
    upload: (files: File[], licence: string) =>
      session &&
      run(
        () => uploadDocuments(session.id, files, licence),
        (r) => setData((d) => (d ? { ...d, uploads: [...r.uploads, ...d.uploads] } : d)),
      ),
    replaceUpload: (u: Upload) => setData((d) => (d ? { ...d, uploads: d.uploads.map((x) => (x.id === u.id ? u : x)) } : d)),
  };
}

export type ResearchState = ReturnType<typeof useResearch>;
