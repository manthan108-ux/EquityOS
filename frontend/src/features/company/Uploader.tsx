"use client";

import { forwardRef, useRef, useState, useImperativeHandle } from "react";

import { Badge, Button, Card, CardHeader, Field, Notice, inputClass, type Tone } from "@/components/ui";
import { titleCase } from "@/lib/format";
import { clarifyUpload, type Requirement, type Upload } from "@/services/research";
import { explain } from "@/features/research/useResearch";

const MAX_NOTE = "PDF, XLSX or CSV · several files at once";
const ACCEPT = ".pdf,.xlsx,.csv";

/** The document-processing status in the user's vocabulary, from what the backend reports. */
export function uploadStage(u: Upload): { label: string; tone: Tone } {
  if (u.status === "RECEIVED") return { label: "Uploaded", tone: "info" };
  if (u.status === "PROCESSING") return { label: "Processing", tone: "info" };
  if (u.status === "NEEDS_CLARIFICATION") return { label: "Needs your answer", tone: "warn" };
  if (u.status === "REJECTED" || u.status === "FAILED") return { label: u.status === "FAILED" ? "Processing failed" : "Rejected", tone: "bad" };
  if (u.status === "DUPLICATE") return { label: "Duplicate of a held document", tone: "neutral" };
  if (u.status === "HELD_PRIVATE") return { label: "Held privately", tone: "info" };
  if (u.conflicts.length > 0) return { label: "Conflict detected", tone: "warn" };
  if (u.validation_status === "FAILED" || u.validation_status === "REJECTED") return { label: "Validation failed", tone: "bad" };
  if (u.validation_status === "PENDING") return { label: "Validation pending", tone: "warn" };
  if (u.status === "ACCEPTED" && u.resolved_requirements.length === 0) return { label: "Extracted, nothing mapped", tone: "warn" };
  if (u.status === "ACCEPTED") return { label: "Validated", tone: "ok" };
  return { label: titleCase(u.status), tone: "neutral" };
}

function ClarifyBox({ u, onDone }: { u: Upload; onDone: (u: Upload) => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const send = (answers: Record<string, string | boolean>) =>
    clarifyUpload(u.id, answers).then(onDone).catch((e) => setError(explain(e).message));
  return (
    <div className="mt-3 space-y-3 rounded-[10px] border border-warn-line bg-warn-soft p-3 text-[13px]">
      {u.ambiguities.map((a) => (
        <div key={a.code}>
          <p className="text-ink">{a.message}</p>
          {a.code === "ISSUER_UNCONFIRMED" && (
            <Button size="sm" className="mt-2" onClick={() => send({ confirm_issuer: true })}>
              Yes, this is this company&apos;s own document
            </Button>
          )}
          {a.code === "DOC_TYPE_UNKNOWN" && (
            <div className="mt-2 flex flex-wrap gap-2">
              {a.options.map((o) => (
                <Button key={o} size="sm" onClick={() => send({ doc_type: o })}>{titleCase(o)}</Button>
              ))}
            </div>
          )}
          {(a.code === "BASIS_UNSTATED" || a.code === "UNIT_UNSTATED") && (
            <div className="mt-2 flex flex-wrap gap-2">
              {a.options.map((o) => (
                <Button key={o} size="sm" onClick={() => send({ [a.code === "BASIS_UNSTATED" ? "basis" : "unit"]: o })}>
                  {a.code === "BASIS_UNSTATED" ? titleCase(o.toLowerCase()) : o === "inr" ? "Rupees" : titleCase(o)}
                </Button>
              ))}
            </div>
          )}
          {a.code === "SOURCE_REFERENCE_REQUIRED" && (
            <div className="mt-2 flex flex-wrap gap-2">
              <input value={text} onChange={(e) => setText(e.target.value)} aria-label="Source reference" placeholder="e.g. Q2 FY27 results, BSE, 30 Oct 2026" className={`${inputClass} max-w-sm`} />
              <Button size="sm" variant="primary" disabled={!text.trim()} onClick={() => send({ source_reference: text.trim() })}>Send</Button>
            </div>
          )}
        </div>
      ))}
      {error && <p role="alert" className="text-bad">{error}</p>}
    </div>
  );
}

function UploadRow({ u, onChange }: { u: Upload; onChange: (u: Upload) => void }) {
  const st = uploadStage(u);
  const meta = [
    u.category && titleCase(u.category),
    u.fiscal_year && `${u.fiscal_year}${u.quarter ? ` ${u.quarter}` : ""}`,
    u.basis?.toLowerCase(),
    u.accounting_standard && u.accounting_standard !== "UNKNOWN" ? u.accounting_standard.replace("_", " ") : null,
    u.issuer_status && `issuer ${u.issuer_status.toLowerCase()}`,
    u.extraction_status && `extraction ${u.extraction_status.toLowerCase()}`,
    u.validation_status && `validation ${u.validation_status.toLowerCase()}`,
    u.version > 1 ? `version ${u.version}` : null,
  ].filter(Boolean);
  return (
    <li className="px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="min-w-0 truncate text-sm font-medium text-ink">{u.filename ?? "file"}</span>
        <Badge tone={st.tone} dot>{st.label}</Badge>
      </div>
      {meta.length > 0 && <p className="mt-1 text-xs text-muted">{meta.join(" · ")}</p>}
      {u.reject_reason && <p className="mt-2 text-xs text-bad">{u.reject_reason}</p>}
      {u.resolved_requirements.length > 0 && <p className="mt-2 text-xs text-ok">Settled requirements: {u.resolved_requirements.map(titleCase).join(", ")}</p>}
      {u.warnings.map((w) => <p key={w} className="mt-1 text-xs text-warn">{w}</p>)}
      {u.conflicts.length > 0 && (
        <p className="mt-2 text-xs text-warn">
          {u.conflicts.length} value(s) disagree with what EquityOS already held. Both are recorded and the stronger source is used.
        </p>
      )}
      {u.steps.length > 0 && (
        <details className="mt-2 text-xs text-muted">
          <summary className="cursor-pointer font-medium">Processing steps</summary>
          <ul className="mt-1 space-y-0.5">
            {u.steps.map((s, i) => (
              <li key={i}><span className={s.ok ? "text-ok" : "text-bad"}>{s.ok ? "✓" : "✗"}</span> {titleCase(s.stage)}: {s.detail}</li>
            ))}
          </ul>
        </details>
      )}
      {u.status === "NEEDS_CLARIFICATION" && <ClarifyBox u={u} onDone={onChange} />}
    </li>
  );
}

export type UploaderHandle = { focus: () => void };

/** Drag-and-drop, multi-file upload, with the processing status of everything uploaded so far. */
const Uploader = forwardRef<
  UploaderHandle,
  {
    uploads: Upload[];
    busy: boolean;
    error: string | null;
    onUpload: (files: File[], licence: string) => Promise<unknown> | null | undefined;
    onChange: (u: Upload) => void;
    forRequirement?: Requirement | null;
    onClearRequirement?: () => void;
  }
>(function Uploader({ uploads, busy, error, onUpload, onChange, forRequirement, onClearRequirement }, ref) {
  const [files, setFiles] = useState<File[]>([]);
  const [licence, setLicence] = useState("PUBLIC_FILING");
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const card = useRef<HTMLDivElement>(null);
  useImperativeHandle(ref, () => ({ focus: () => { card.current?.scrollIntoView({ behavior: "smooth", block: "start" }); input.current?.focus(); } }));

  const add = (list: FileList | File[] | null) => {
    if (!list) return;
    const allowed = Array.from(list).filter((f) => /\.(pdf|xlsx|csv)$/i.test(f.name));
    setFiles((cur) => [...cur, ...allowed.filter((f) => !cur.some((c) => c.name === f.name && c.size === f.size))]);
  };

  return (
    <div ref={card} id="uploader" className="scroll-mt-24">
    <Card aria-label="Upload documents">
      <CardHeader title="Upload supporting documents" subtitle="Each file is checked by its content, not its name. Nothing is trusted until it passes validation." />
      <div className="space-y-4 p-5">
        {forRequirement && (
          <Notice tone="brand" title={`Uploading for: ${forRequirement.document}`}>
            {forRequirement.preferred_period} · {forRequirement.basis_required}.{" "}
            <button type="button" className="font-medium text-brand underline" onClick={onClearRequirement}>Clear</button>
          </Notice>
        )}
        <div
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); }}
          className={`flex flex-col items-center rounded-[12px] border-2 border-dashed px-6 py-8 text-center transition-colors ${over ? "border-brand bg-brand-soft" : "border-line-strong bg-surface-2"}`}
        >
          <svg aria-hidden viewBox="0 0 24 24" className="h-8 w-8 text-faint" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V4m0 0L7 9m5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" /></svg>
          <p className="mt-2 text-sm font-medium text-ink">Drag files here, or</p>
          <input ref={input} id="file-input" type="file" multiple accept={ACCEPT} className="sr-only" onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
          <label htmlFor="file-input" className="mt-2 inline-flex h-10 cursor-pointer items-center rounded-[8px] border border-line-strong bg-surface px-4 text-sm font-medium text-ink hover:bg-surface-2 focus-within:outline-2">
            Browse files
          </label>
          <p className="mt-2 text-xs text-muted">{MAX_NOTE}. The CSV/XLSX format is at /api/research/template.</p>
        </div>

        {files.length > 0 && (
          <ul className="divide-y divide-line rounded-[10px] border border-line" aria-label="Files ready to upload">
            {files.map((f) => (
              <li key={f.name + f.size} className="flex items-center justify-between gap-3 px-3 py-2 text-[13px]">
                <span className="min-w-0 truncate text-ink">{f.name}</span>
                <span className="flex shrink-0 items-center gap-3 text-xs text-muted">
                  {(f.size / 1024 / 1024).toFixed(2)} MB
                  <button type="button" aria-label={`Remove ${f.name}`} className="font-medium text-bad hover:underline" onClick={() => setFiles((cur) => cur.filter((x) => x !== f))}>Remove</button>
                </span>
              </li>
            ))}
          </ul>
        )}

        <div className="flex flex-wrap items-end gap-3">
          <Field label="These documents are">
            <select value={licence} onChange={(e) => setLicence(e.target.value)} className={`${inputClass} w-80`}>
              <option value="PUBLIC_FILING">Published filings (company, exchange or regulator)</option>
              <option value="USER_LICENSED">Private or licensed: keep private, do not share</option>
            </select>
          </Field>
          <Button variant="primary" loading={busy} disabledReason={files.length ? undefined : "Choose at least one file first."} onClick={() => {
              Promise.resolve(onUpload(files, licence)).then((r) => r && setFiles([]));
            }}
          >
            {busy ? "Uploading…" : files.length ? `Upload ${files.length} file${files.length === 1 ? "" : "s"}` : "Upload files"}
          </Button>
        </div>
        {licence !== "PUBLIC_FILING" && <Notice tone="info">Private documents are stored in your vault but are not read into the shared evidence, so they cannot settle a requirement.</Notice>}
        {error && <Notice tone="bad" title="Upload failed">{error}</Notice>}
      </div>

      {uploads.length > 0 && (
        <>
          <div className="border-t border-line px-5 py-3 text-xs font-medium tracking-wide text-muted uppercase">Uploaded documents ({uploads.length})</div>
          <ul className="divide-y divide-line border-t border-line">
            {uploads.map((u) => <UploadRow key={u.id} u={u} onChange={onChange} />)}
          </ul>
        </>
      )}
    </Card>
    </div>
  );
});

export default Uploader;
