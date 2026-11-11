"use client";

import { Badge, Button, Card, CardHeader, EmptyState, Notice, Stat, type Tone } from "@/components/ui";
import { fmtValue, titleCase } from "@/lib/format";
import type { MissingAssessment, Requirement, Upload } from "@/services/research";

const ACTION: Record<string, string> = {
  UPLOAD: "Upload this document to settle the requirement.",
  CHANGE_BASIS_OR_UPLOAD: "Figures exist on the other accounting basis. Change the basis preference, or upload the statements on the basis you chose.",
  EXTRACT_HELD: "EquityOS already holds a matching document. Resume research to process it.",
  RECALCULATE: "The inputs exist. Resume research to recalculate.",
  REFRESH_MARKET_DATA: "Needs fresh exchange price data. EquityOS refreshes this itself; nothing to upload.",
  NOT_RESOLVABLE_BY_UPLOAD: "No extractor can read this figure from a document yet, so it stays disclosed as unavailable.",
};
const FORMATS = "PDF, XLSX or CSV";

const LEVEL: Record<Requirement["importance"], { label: string; tone: Tone; blurb: string }> = {
  CRITICAL: { label: "Critical", tone: "bad", blurb: "Research cannot be complete without these." },
  IMPORTANT: { label: "Important", tone: "warn", blurb: "The report is weaker without these; the gaps are disclosed." },
  OPTIONAL: { label: "Optional", tone: "neutral", blurb: "Never block research." },
};


/** Where a requirement stands, in the words of the document workflow. */
function statusOf(m: Requirement): { label: string; tone: Tone } {
  if (m.status === "AVAILABLE") return { label: "Available and validated", tone: "ok" };
  if (m.status === "STALE") return { label: "Out of date", tone: "warn" };
  if (m.action === "EXTRACT_HELD") return { label: "Held, not yet processed", tone: "info" };
  if (!m.upload_helps) return { label: "Cannot be settled by upload", tone: "neutral" };
  return { label: m.importance === "CRITICAL" ? "Critical: missing" : "Missing", tone: m.importance === "CRITICAL" ? "bad" : "warn" };
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[110px_1fr] gap-2 py-1.5 text-[13px]">
      <dt className="text-muted">{k}</dt>
      <dd className="text-ink">{children}</dd>
    </div>
  );
}

/** The document without a trailing clarification such as "(borrowings are required)". */
const docName = (s: string) => {
  const t = s.replace(/\s*\([^)]*\)\s*$/, "").trim() || s;
  return t.charAt(0).toUpperCase() + t.slice(1);
};
const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
/** The anchor of the card that holds a requirement (cards are grouped by the document that settles them). */
export const docAnchor = (m: Pick<Requirement, "document">) => `doc-${slug(docName(m.document))}`;

const uniq = (xs: string[]) => [...new Set(xs.filter(Boolean))];

/** One card per document, listing every figure it would settle. */
function DocumentCard({ group, onUpload }: { group: Requirement[]; onUpload: (m: Requirement) => void }) {
  const first = group[0];
  const sts = group.map(statusOf);
  const worst = sts.find((x) => x.tone === "bad") ?? sts.find((x) => x.tone === "warn") ?? sts[0];
  const uploadable = group.some((m) => m.upload_helps);
  const enables = uniq(group.flatMap((m) => m.sections.map((x) => titleCase(x))));
  const otherBasis = group.filter((m) => m.other_basis_available);
  return (
    <li id={docAnchor(first)} className="scroll-mt-24 rounded-[12px] border border-line bg-surface p-5 target:ring-2 target:ring-brand/30">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="text-[15px] font-semibold text-ink">{docName(first.document)}</h4>
          <p className="mt-0.5 text-[13px] text-muted">{uniq(group.map((m) => m.why_it_matters))[0]}</p>
        </div>
        <Badge tone={worst.tone} dot>{worst.label}</Badge>
      </div>
      <ul className="mt-3 flex flex-wrap gap-1.5" aria-label="Figures this document settles">
        {group.map((m) => (
          <li key={m.id} className="rounded-full border border-line bg-surface-2 px-2.5 py-0.5 text-xs text-ink" title={m.affects}>{m.label}</li>
        ))}
      </ul>
      {otherBasis.length > 0 && (
        <p className="mt-3 rounded-md bg-warn-soft px-3 py-2 text-xs text-ink">
          {otherBasis.map((m) => m.label).join(", ")}: available on the {otherBasis[0].other_basis_available?.basis?.toLowerCase()} basis for {otherBasis[0].other_basis_available?.period_end}. It is not used unless you change the basis preference.
        </p>
      )}
      <details className="mt-3 border-t border-line pt-2">
        <summary className="cursor-pointer text-[13px] font-medium text-brand">Requirements: period, basis, formats, source</summary>
        <dl className="mt-1 divide-y divide-line">
          <Row k="Enables">{enables.join(" · ")}</Row>
          <Row k="Period">{uniq(group.map((m) => m.preferred_period)).join("; ")}</Row>
          <Row k="Basis">{uniq(group.map((m) => m.basis_required)).join("; ")}</Row>
          <Row k="Accepted">{FORMATS}{uniq(group.flatMap((m) => m.document_types)).length > 0 && <span className="text-muted"> · document types: {uniq(group.flatMap((m) => m.document_types)).map(titleCase).join(", ")}</span>}</Row>
          <Row k="Source">{uniq(group.map((m) => m.official_source)).join("; ")}{first.alternative && <span className="block text-muted">Alternative: {first.alternative}</span>}</Row>
        </dl>
        <ul className="mt-2 space-y-1 text-xs text-muted">
          {group.map((m) => (
            <li key={m.id}><span className="font-medium text-ink">{m.label}:</span> {m.reason ?? "no reason recorded"}. {ACTION[m.action] ?? m.action}</li>
          ))}
        </ul>
      </details>
      <div className="mt-4 flex justify-end">
        <Button
          variant={first.importance === "CRITICAL" ? "primary" : "secondary"}
          onClick={() => onUpload(first)}
          disabledReason={uploadable ? undefined : "No document upload can settle these requirements."}
        >
          Upload document
        </Button>
      </div>
    </li>
  );
}

const groupByDocument = (list: Requirement[]) => {
  const m = new Map<string, Requirement[]>();
  for (const r of list) m.set(docName(r.document), [...(m.get(docName(r.document)) ?? []), r]);
  return [...m.values()];
};

/** The most visible part of the product: what documents are needed, why, and how to supply them. */
export default function DocumentRequirements({
  missing,
  uploads,
  onUpload,
}: {
  missing: MissingAssessment;
  uploads: Upload[];
  onUpload: (m?: Requirement) => void;
}) {
  const crit = missing.missing.CRITICAL;
  const imp = missing.missing.IMPORTANT;
  const opt = missing.missing.OPTIONAL;
  const validated = uploads.filter((u) => u.status === "ACCEPTED").length;
  const blocked = missing.eligibility === "BLOCKED";
  const jump = (m: Requirement) => document.getElementById(docAnchor(m))?.scrollIntoView({ behavior: "smooth", block: "start" });

  return (
    <section aria-labelledby="doc-req" className="space-y-5">
      <Card>
        <CardHeader
          id="doc-req"
          title="Documents Required for Research"
          subtitle="What is missing, why it matters, and exactly what to upload."
          actions={<Button variant="primary" onClick={() => onUpload()}>Upload documents</Button>}
        />
        <dl className="grid grid-cols-2 gap-5 p-5 md:grid-cols-5">
          <Stat label="Critical missing" value={crit.length} tone={crit.length ? "bad" : "ok"} />
          <Stat label="Important missing" value={imp.length} tone={imp.length ? "warn" : "ok"} />
          <Stat label="Uploaded" value={uploads.length} hint="in this session" />
          <Stat label="Validated" value={validated} hint="accepted uploads" tone={validated ? "ok" : undefined} />
          <Stat label="Research blockers" value={blocked ? "Blocked" : crit.length ? `${crit.length} critical` : "None"} tone={blocked || crit.length ? "bad" : "ok"} />
        </dl>
        {(crit.length > 0 || imp.length > 0) && (
          <div className="border-t border-line px-5 py-4">
            <p className="mb-2 text-xs font-medium tracking-wide text-muted uppercase">Jump to a requirement</p>
            <ul className="flex flex-wrap gap-2">
              {[...crit, ...imp].map((m) => (
                <li key={m.id}>
                  <button type="button" onClick={() => jump(m)} className={`rounded-full border px-3 py-1 text-xs font-medium hover:bg-surface-2 ${m.importance === "CRITICAL" ? "border-bad-line text-bad" : "border-warn-line text-warn"}`}>
                    {m.label}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Card>

      {blocked && (
        <Notice tone="bad" title="No report can be written yet">
          EquityOS holds no validated revenue or profit for this company. Upload its financial results to begin.
        </Notice>
      )}

      {(["CRITICAL", "IMPORTANT", "OPTIONAL"] as const).map((level) => {
        const list = missing.missing[level];
        if (!list.length) return null;
        return (
          <div key={level}>
            <div className="mb-2 flex items-baseline gap-3">
              <h3 className="text-sm font-semibold text-ink">{LEVEL[level].label} documents ({groupByDocument(list).length}) <span className="font-normal text-muted">· {list.length} figure{list.length === 1 ? "" : "s"}</span></h3>
              <span className="text-xs text-muted">{LEVEL[level].blurb}</span>
            </div>
            <ul className="space-y-3">
              {groupByDocument(list).map((g) => (
                <DocumentCard key={docName(g[0].document)} group={g} onUpload={onUpload} />
              ))}
            </ul>
          </div>
        );
      })}

      {!crit.length && !imp.length && !opt.length && (
        <EmptyState title="Nothing is missing">Every requirement for this research profile is met by validated evidence.</EmptyState>
      )}

      {missing.resolved.length > 0 && (
        <Card>
          <CardHeader title={`Already available (${missing.resolved.length})`} subtitle="Validated evidence EquityOS already holds. Nothing to upload for these." />
          <ul className="grid gap-px bg-line sm:grid-cols-2">
            {missing.resolved.map((x) => (
              <li key={x.id} className="flex items-start justify-between gap-3 bg-surface px-5 py-3 text-[13px]">
                <span>
                  <span className="block font-medium text-ink">{x.label}</span>
                  <span className="text-xs text-muted">{[x.found.period_end, x.found.basis?.toLowerCase(), x.found.page ? `page ${x.found.page}` : null].filter(Boolean).join(" · ")}</span>
                </span>
                <span className="tnum text-right font-medium text-ink">{fmtValue(x.found.value, x.found.unit)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {missing.documents_held.length > 0 && (
        <Card>
          <CardHeader title={`Documents EquityOS already holds (${missing.documents_held.length})`} />
          <ul className="divide-y divide-line">
            {missing.documents_held.map((d, i) => (
              <li key={i} className="flex flex-wrap items-center justify-between gap-2 px-5 py-3 text-[13px]">
                <span className="min-w-0">
                  <span className="block truncate font-medium text-ink">{d.title ?? "Untitled document"}</span>
                  <span className="text-xs text-muted">{titleCase(d.doc_type)} · {d.financial_period ?? "period unknown"}</span>
                </span>
                <Badge tone={d.extraction === "COMPLETED" ? "ok" : "neutral"}>Extraction {titleCase(d.extraction).toLowerCase()}</Badge>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </section>
  );
}
