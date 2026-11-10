"use client";

import { useEffect, useRef, useState } from "react";

import AccessKey, { useApiKey } from "@/features/research/AccessKey";
import ReportView from "@/features/research/ReportView";
import {
  ApiError,
  clarifyUpload,
  continueWithAvailable,
  fetchMissing,
  fetchReport,
  fetchReports,
  fetchResearchReadiness,
  fetchSession,
  fetchUploads,
  generateReport,
  resumeResearch,
  startResearch,
  uploadDocuments,
  type MissingAssessment,
  type Readiness,
  type Report,
  type ReportRow,
  type Requirement,
  type SessionStatus,
  type Upload,
} from "@/services/research";

type Bundle = {
  readiness: Readiness;
  missing: MissingAssessment;
  uploads: Upload[];
  reports: ReportRow[];
};

const msg = (e: unknown) =>
  e instanceof ApiError
    ? e.status === 401
      ? "The access key was not accepted."
      : e.status === 503
        ? "Research access is not configured on the server (RESEARCH_API_KEYS)."
        : e.status === 429
          ? "Too many requests: wait a moment and try again."
          : e.message
    : "Could not reach the server.";

const IMPORTANCE: Record<string, string> = {
  CRITICAL: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
  IMPORTANT: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  OPTIONAL: "bg-zinc-200 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
};
const ELIGIBILITY: Record<string, string> = {
  AUTOMATIC: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  ASSISTED: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  BLOCKED: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
};
const UPLOAD_BADGE: Record<string, string> = {
  ACCEPTED: "text-emerald-700 dark:text-emerald-300",
  HELD_PRIVATE: "text-sky-700 dark:text-sky-300",
  NEEDS_CLARIFICATION: "text-amber-700 dark:text-amber-300",
  REJECTED: "text-red-700 dark:text-red-300",
  FAILED: "text-red-700 dark:text-red-300",
  DUPLICATE: "text-zinc-500",
};
const ACTION: Record<string, string> = {
  UPLOAD: "Upload the document below",
  EXTRACT_HELD: "EquityOS already holds a matching document: resume to process it",
  RECALCULATE: "Inputs exist: resume to recalculate",
  REFRESH_MARKET_DATA: "Needs fresh exchange price data (refreshed by EquityOS, not by you)",
  NOT_RESOLVABLE_BY_UPLOAD:
    "No extractor can read this figure from a document yet: it stays disclosed as unavailable",
};

const loadAll = async (id: string): Promise<Bundle> => {
  const [readiness, missing, uploads, reports] = await Promise.all([
    fetchResearchReadiness(id),
    fetchMissing(id),
    fetchUploads(id),
    fetchReports(id),
  ]);
  return { readiness, missing, uploads, reports };
};

function Steps({ session }: { session: SessionStatus }) {
  return (
    <ol className="space-y-1 text-sm" aria-label="Research progress">
      {session.steps.map((s) => (
        <li key={s.step} className="flex items-center gap-2">
          <span
            className={`h-2.5 w-2.5 rounded-full ${
              s.status === "DONE"
                ? "bg-emerald-500"
                : s.status === "RUNNING"
                  ? "animate-pulse bg-sky-500"
                  : s.status === "FAILED"
                    ? "bg-red-500"
                    : s.status === "SKIPPED"
                      ? "bg-zinc-400"
                      : "bg-zinc-200 dark:bg-zinc-700"
            }`}
          />
          <span>{s.label}</span>
          {s.resumed_without_rerun && (
            <span className="text-xs text-zinc-500">(already done, not repeated)</span>
          )}
          {s.status === "SKIPPED" && <span className="text-xs text-zinc-500">(not needed)</span>}
          {s.error && <span className="text-xs text-red-600">{s.error}</span>}
        </li>
      ))}
    </ol>
  );
}

function ReadinessPanel({ r }: { r: Readiness }) {
  return (
    <section className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">Research readiness</h3>
        <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${ELIGIBILITY[r.eligibility]}`}>
          {r.eligibility === "AUTOMATIC"
            ? "Automatic research"
            : r.eligibility === "ASSISTED"
              ? "Assisted research"
              : "Blocked"}
        </span>
      </div>
      <p className="mt-1 text-xs text-zinc-500">
        This measures the evidence EquityOS holds, not how good the company is.
      </p>
      <div className="mt-3 flex items-end gap-4">
        <div>
          <div className="text-3xl font-semibold">{r.score}</div>
          <div className="text-xs text-zinc-500">of 100 · {r.profile_label}</div>
        </div>
        <dl className="grid grid-cols-3 gap-4 text-sm">
          <div>
            <dt className="text-xs text-zinc-500">Critical missing</dt>
            <dd className="font-medium">{r.counts.critical_missing}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Important missing</dt>
            <dd className="font-medium">{r.counts.important_missing}</dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500">Evidence confidence</dt>
            <dd className="font-medium">{r.confidence.toLowerCase()}</dd>
          </div>
        </dl>
      </div>
      <ul className="mt-3 space-y-1.5">
        {Object.entries(r.components).map(([k, c]) => (
          <li key={k} className="text-sm">
            <div className="flex justify-between">
              <span>{c.label}</span>
              <span className="text-zinc-500">{c.score === null ? "n/a" : c.score}</span>
            </div>
            <div className="h-1.5 rounded bg-zinc-100 dark:bg-zinc-800">
              <div
                className={`h-1.5 rounded ${
                  c.score !== null && c.score < r.thresholds.min_component ? "bg-red-500" : "bg-emerald-500"
                }`}
                style={{ width: `${c.score ?? 0}%` }}
              />
            </div>
            <div className="text-xs text-zinc-500">{c.detail}</div>
          </li>
        ))}
      </ul>
      {r.reasons.length > 0 && (
        <ul className="mt-3 list-disc pl-5 text-sm text-zinc-700 dark:text-zinc-300">
          {r.reasons.map((x) => (
            <li key={x}>{x}</li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-xs text-zinc-500">
        Automatic research needs: no critical item missing, score ≥ {r.thresholds.min_score}, every
        component ≥ {r.thresholds.min_component}, and no open validation error. One rule alone is
        never enough.
      </p>
    </section>
  );
}

function MissingItem({ m }: { m: Requirement }) {
  return (
    <li className="rounded-lg border border-zinc-200 p-3 text-sm dark:border-zinc-800">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{m.label}</span>
        <span className={`rounded-full px-2 py-0.5 text-xs ${IMPORTANCE[m.importance]}`}>{m.importance.toLowerCase()}</span>
        {m.status === "STALE" && <span className="text-xs text-amber-700">out of date</span>}
      </div>
      <p className="mt-1">{m.why_it_matters}</p>
      <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs text-zinc-600 sm:grid-cols-2 dark:text-zinc-400">
        <div>
          <dt className="inline font-medium">Affects: </dt>
          <dd className="inline">
            {m.affects} (report: {m.sections.join(", ").replace(/_/g, " ")})
          </dd>
        </div>
        <div>
          <dt className="inline font-medium">Document: </dt>
          <dd className="inline">{m.document}</dd>
        </div>
        <div>
          <dt className="inline font-medium">Period: </dt>
          <dd className="inline">{m.preferred_period}</dd>
        </div>
        <div>
          <dt className="inline font-medium">Basis: </dt>
          <dd className="inline">{m.basis_required}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="inline font-medium">Official source: </dt>
          <dd className="inline">{m.official_source}</dd>
        </div>
        {m.alternative && (
          <div className="sm:col-span-2">
            <dt className="inline font-medium">Alternative: </dt>
            <dd className="inline">{m.alternative}</dd>
          </div>
        )}
      </dl>
      {m.reason && <p className="mt-2 text-xs text-zinc-500">Why it is missing: {m.reason}</p>}
      <p className="mt-1 text-xs font-medium">{ACTION[m.action] ?? m.action}</p>
    </li>
  );
}

function ClarifyBox({ u, onDone }: { u: Upload; onDone: (u: Upload) => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const send = (answers: Record<string, string | boolean>) =>
    clarifyUpload(u.id, answers)
      .then(onDone)
      .catch((e) => setError(msg(e)));
  return (
    <div className="mt-2 space-y-2 rounded-md bg-amber-50 p-2 text-xs dark:bg-amber-950/40">
      {u.ambiguities.map((a) => (
        <div key={a.code}>
          <p>{a.message}</p>
          {a.code === "ISSUER_UNCONFIRMED" && (
            <button type="button" className="mt-1 rounded border px-2 py-1" onClick={() => send({ confirm_issuer: true })}>
              Yes, this is this company&apos;s own document
            </button>
          )}
          {a.code === "DOC_TYPE_UNKNOWN" &&
            a.options.map((o) => (
              <button key={o} type="button" className="mr-1 mt-1 rounded border px-2 py-1" onClick={() => send({ doc_type: o })}>
                {o.replace(/_/g, " ")}
              </button>
            ))}
          {a.code === "SOURCE_REFERENCE_REQUIRED" && (
            <div className="mt-1 flex gap-1">
              <input
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="e.g. Q2 FY27 results, BSE, 30 Oct 2026"
                className="w-72 rounded border px-2 py-1 dark:bg-zinc-900"
              />
              <button
                type="button"
                disabled={!text.trim()}
                className="rounded border px-2 py-1 disabled:opacity-40"
                onClick={() => send({ source_reference: text.trim() })}
              >
                Send
              </button>
            </div>
          )}
        </div>
      ))}
      {error && <p className="text-red-600">{error}</p>}
    </div>
  );
}

function UploadPanel({
  sessionId,
  uploads,
  onChange,
}: {
  sessionId: string;
  uploads: Upload[];
  onChange: (uploads: Upload[]) => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [licence, setLicence] = useState("PUBLIC_FILING");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const send = () => {
    setBusy(true);
    setError("");
    uploadDocuments(sessionId, files, licence)
      .then((r) => {
        setFiles([]);
        onChange([...r.uploads, ...uploads]);
      })
      .catch((e) => setError(msg(e)))
      .finally(() => setBusy(false));
  };
  const replace = (u: Upload) => onChange(uploads.map((x) => (x.id === u.id ? u : x)));

  return (
    <section className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
      <h3 className="font-semibold">Upload supporting documents</h3>
      <p className="mt-1 text-xs text-zinc-500">
        PDF, XLSX or CSV (the CSV/XLSX format is at /api/research/template). Several files at once
        are fine. Each file is checked by its content, not its name; nothing is trusted until it
        passes validation.
      </p>
      <div className="mt-3 flex flex-wrap items-end gap-3 text-sm">
        <input
          type="file"
          multiple
          accept=".pdf,.xlsx,.csv"
          onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
        />
        <label>
          <span className="block text-xs text-zinc-500">These documents are</span>
          <select
            value={licence}
            onChange={(e) => setLicence(e.target.value)}
            className="rounded border px-2 py-1 dark:bg-zinc-900"
          >
            <option value="PUBLIC_FILING">published filings (company / exchange / regulator)</option>
            <option value="USER_LICENSED">private or licensed: keep private, do not share</option>
          </select>
        </label>
        <button
          type="button"
          disabled={!files.length || busy}
          onClick={send}
          className="rounded-lg bg-zinc-900 px-3 py-1.5 font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
        >
          {busy ? "Uploading…" : `Upload ${files.length || ""} file${files.length === 1 ? "" : "s"}`}
        </button>
      </div>
      {licence !== "PUBLIC_FILING" && (
        <p className="mt-2 text-xs text-sky-700 dark:text-sky-300">
          Private documents are stored in your vault but are not read into the shared evidence, so
          they cannot settle a requirement.
        </p>
      )}
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      {uploads.length > 0 && (
        <ul className="mt-4 space-y-2">
          {uploads.map((u) => (
            <li key={u.id} className="rounded-md border border-zinc-200 p-2 text-sm dark:border-zinc-800">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium">{u.filename ?? "file"}</span>
                <span className={`text-xs font-semibold ${UPLOAD_BADGE[u.status] ?? ""}`}>
                  {u.status.replace(/_/g, " ").toLowerCase()}
                </span>
              </div>
              <div className="text-xs text-zinc-500">
                {[
                  u.category?.replace(/_/g, " "),
                  u.fiscal_year && `${u.fiscal_year}${u.quarter ? ` ${u.quarter}` : ""}`,
                  u.basis?.toLowerCase(),
                  u.accounting_standard && u.accounting_standard !== "UNKNOWN" ? u.accounting_standard.replace("_", " ") : null,
                  u.issuer_status && `issuer ${u.issuer_status.toLowerCase()}`,
                  u.validation_status && `validation ${u.validation_status.toLowerCase()}`,
                  u.version > 1 ? `version ${u.version}` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </div>
              {u.reject_reason && <p className="mt-1 text-xs text-red-700 dark:text-red-300">{u.reject_reason}</p>}
              {u.resolved_requirements.length > 0 && (
                <p className="mt-1 text-xs text-emerald-700 dark:text-emerald-300">
                  Settled: {u.resolved_requirements.join(", ").replace(/_/g, " ")}
                </p>
              )}
              {u.warnings.map((w) => (
                <p key={w} className="mt-1 text-xs text-amber-700 dark:text-amber-300">{w}</p>
              ))}
              {u.conflicts.length > 0 && (
                <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
                  {u.conflicts.length} value(s) disagree with what EquityOS already held; both are
                  recorded and the stronger source is used.
                </p>
              )}
              {u.status === "NEEDS_CLARIFICATION" && <ClarifyBox u={u} onDone={replace} />}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function ResearchWorkspace({
  companyId,
  companyName,
  sessionId,
}: {
  companyId: string;
  companyName: string;
  sessionId?: string;
}) {
  const key = useApiKey();
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [data, setData] = useState<Bundle | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const attached = useRef(false);

  // Open an existing (saved) session when one is given.
  useEffect(() => {
    if (!sessionId || !key || attached.current) return;
    attached.current = true;
    fetchSession(sessionId).then(setSession).catch((e) => setError(msg(e)));
  }, [sessionId, key]);

  const sid = session?.id;
  const running = session?.status === "RUNNING" || session?.status === "CREATED";

  // While the research runs, poll its progress.
  useEffect(() => {
    if (!sid || !running) return;
    const t = setTimeout(() => {
      fetchSession(sid).then(setSession).catch((e) => setError(msg(e)));
    }, 1500);
    return () => clearTimeout(t);
  }, [sid, running, session]);

  // When it settles (or changes), load readiness, the missing-data assessment, uploads, reports.
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
      .catch((e) => live && setError(msg(e)));
    return () => {
      live = false;
    };
  }, [settled, sid]);

  // While uploads are being processed in the background, poll them; refresh everything after.
  const processing = data?.uploads.some((u) => u.status === "RECEIVED" || u.status === "PROCESSING");
  useEffect(() => {
    if (!sid || !processing) return;
    const t = setTimeout(() => {
      fetchSession(sid).then(setSession).catch((e) => setError(msg(e)));
      fetchUploads(sid)
        .then((uploads) => setData((d) => (d ? { ...d, uploads } : d)))
        .catch((e) => setError(msg(e)));
    }, 2000);
    return () => clearTimeout(t);
  }, [sid, processing, data]);

  const act = (fn: () => Promise<SessionStatus | Report>, isReport = false) => {
    setBusy(true);
    setError("");
    fn()
      .then((r) => {
        if (isReport) {
          setReport(r as Report);
          if (sid) fetchReports(sid).then((reports) => setData((d) => (d ? { ...d, reports } : d)));
        } else setSession(r as SessionStatus);
      })
      .catch((e) => setError(msg(e)))
      .finally(() => setBusy(false));
  };

  const r = data?.readiness;
  const miss = data?.missing;

  return (
    <section aria-label="Research" className="mt-6 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Research: {companyName}</h2>
        <AccessKey />
      </div>
      {error && (
        <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">
          {error}
        </p>
      )}
      {!session && (
        <div>
          <button
            type="button"
            disabled={!key || busy}
            onClick={() => act(() => startResearch(companyId))}
            className="rounded-lg bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
          >
            {busy ? "Starting…" : "Start research"}
          </button>
          <p className="mt-2 text-xs text-zinc-500">
            EquityOS first checks whether it holds enough validated evidence. If it does, research
            and the report run automatically; if not, it tells you exactly what is missing.
          </p>
        </div>
      )}
      {session && (
        <>
          <div className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
            <div className="mb-2 flex items-center justify-between">
              <h3 className="font-semibold">Progress</h3>
              <span className="text-xs text-zinc-500">{session.status.replace(/_/g, " ").toLowerCase()}</span>
            </div>
            <Steps session={session} />
            {session.status === "FAILED" && (
              <div className="mt-3 text-sm">
                <p className="text-red-700 dark:text-red-300">{session.last_error}</p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => act(() => resumeResearch(session.id))}
                  className="mt-2 rounded border px-3 py-1"
                >
                  Retry from the failed step
                </button>
              </div>
            )}
          </div>

          {r && <ReadinessPanel r={r} />}

          {r && miss && r.eligibility !== "AUTOMATIC" && (
            <section className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
              <h3 className="font-semibold">Missing data assessment</h3>
              {r.eligibility === "BLOCKED" ? (
                <p className="mt-1 text-sm text-red-700 dark:text-red-300">
                  EquityOS holds no validated revenue or profit for this company, so no report can be
                  written yet. Upload its financial results to begin.
                </p>
              ) : (
                <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">
                  Choose: generate a report now with what EquityOS has (every gap is disclosed and no
                  figure is estimated), or upload the documents below and resume.
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy || !miss.options.continue_with_available}
                  onClick={() => act(() => continueWithAvailable(session.id))}
                  className="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
                >
                  Generate with what is available
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => act(() => resumeResearch(session.id))}
                  className="rounded-lg border px-3 py-1.5 text-sm"
                >
                  Resume research
                </button>
              </div>
              {(["CRITICAL", "IMPORTANT", "OPTIONAL"] as const).map(
                (level) =>
                  miss.missing[level].length > 0 && (
                    <div key={level} className="mt-4">
                      <h4 className="text-sm font-semibold">
                        {level[0] + level.slice(1).toLowerCase()} ({miss.missing[level].length})
                      </h4>
                      <ul className="mt-2 space-y-2">
                        {miss.missing[level].map((m) => (
                          <MissingItem key={m.id} m={m} />
                        ))}
                      </ul>
                    </div>
                  ),
              )}
              {miss.resolved.length > 0 && (
                <details className="mt-4 text-sm">
                  <summary className="cursor-pointer font-medium">
                    Already available and validated ({miss.resolved.length}): nothing to upload for these
                  </summary>
                  <ul className="mt-2 grid gap-1 text-xs text-zinc-600 sm:grid-cols-2 dark:text-zinc-400">
                    {miss.resolved.map((x) => (
                      <li key={x.id}>
                        {x.label} · {x.found.period_end ?? ""} {x.found.basis?.toLowerCase() ?? ""}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
              {miss.documents_held.length > 0 && (
                <details className="mt-3 text-sm">
                  <summary className="cursor-pointer font-medium">
                    Documents EquityOS already holds ({miss.documents_held.length})
                  </summary>
                  <ul className="mt-2 text-xs text-zinc-600 dark:text-zinc-400">
                    {miss.documents_held.map((d, i) => (
                      <li key={i}>
                        {d.doc_type.replace(/_/g, " ")} · {d.financial_period ?? "period unknown"} · {d.title ?? "untitled"}{" "}
                        · extraction {d.extraction.toLowerCase().replace(/_/g, " ")}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </section>
          )}

          {r && data && (
            <UploadPanel
              sessionId={session.id}
              uploads={data.uploads}
              onChange={(uploads) => setData({ ...data, uploads })}
            />
          )}

          {r && (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={busy || r.eligibility !== "AUTOMATIC"}
                title={r.eligibility === "AUTOMATIC" ? "" : "Needs every critical requirement met"}
                onClick={() => act(() => generateReport(session.id, "COMPLETE"), true)}
                className="rounded-lg bg-emerald-700 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40"
              >
                Generate complete report
              </button>
              <button
                type="button"
                disabled={busy || r.eligibility === "BLOCKED"}
                onClick={() => act(() => generateReport(session.id, "AVAILABLE_EVIDENCE"), true)}
                className="rounded-lg border px-3 py-1.5 text-sm disabled:opacity-40"
              >
                Generate available-evidence report
              </button>
            </div>
          )}

          {data && data.reports.length > 0 && (
            <section className="rounded-lg border border-zinc-200 p-4 dark:border-zinc-800">
              <h3 className="font-semibold">Report versions</h3>
              <ul className="mt-2 flex flex-wrap gap-2 text-sm">
                {data.reports.map((rp) => (
                  <li key={rp.id}>
                    <button
                      type="button"
                      onClick={() => fetchReport(rp.id).then(setReport).catch((e) => setError(msg(e)))}
                      className={`rounded border px-2 py-1 ${report?.id === rp.id ? "bg-zinc-100 dark:bg-zinc-800" : ""}`}
                    >
                      v{rp.version} · {rp.mode === "COMPLETE" ? "complete" : "available evidence"} ·{" "}
                      {rp.missing_critical} critical gap(s) · {rp.created_at?.slice(0, 10)}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {report && <ReportView report={report} />}
        </>
      )}
    </section>
  );
}
