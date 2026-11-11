"use client";

import { BarChart, LineChart, type Series } from "@/components/charts";
import { Badge, Card, CardHeader, EmptyState, Notice } from "@/components/ui";
import { fmtCompact, fmtValue, pct, titleCase, toNumber } from "@/lib/format";
import type { Analysis, MissingAssessment, Report, TrendRow } from "@/services/research";

const CR = 1e7;

const provenance = (p: TrendRow["points"][number]) =>
  [`evidence #${p.evidence_id}`, p.document_id ? `document ${p.document_id.slice(0, 8)}` : null, p.page ? `page ${p.page}` : null, p.kind === "FACT" ? "stated in a filing" : "derived by EquityOS"].filter(Boolean).join(" · ");

function change(row: TrendRow) {
  const c = row.changes[row.changes.length - 1];
  if (!c) return null;
  const v = Number(c.value);
  const good = row.better ? (v > 0) === (row.better === "higher") : null;
  return { text: c.type === "pct" ? pct(c.value, 1, true) : `${v > 0 ? "+" : ""}${v.toFixed(2)} pts`, period: c.period, tone: good === null ? "text-muted" : Math.abs(v) < 0.001 ? "text-muted" : good ? "text-ok" : "text-bad" };
}

function MetricCard({ row }: { row: TrendRow }) {
  const p = row.points[row.points.length - 1];
  const ch = change(row);
  return (
    <div className="rounded-[10px] border border-line bg-surface p-4" title={provenance(p)}>
      <p className="text-xs font-medium tracking-wide text-muted uppercase">{row.label}</p>
      <p className="tnum mt-1 text-xl font-semibold text-ink">{fmtValue(p.value, p.unit)}</p>
      <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted">
        <span>{p.fy}</span>
        {ch && <span className={`tnum font-medium ${ch.tone}`}>{ch.text} <span className="font-normal text-muted">vs prior year</span></span>}
      </p>
      <p className="mt-1 text-[11px] text-faint">{(row.basis ?? "").toLowerCase()} {row.basis_note ? "· other basis, see note below" : ""} · {provenance(p)}</p>
    </div>
  );
}

const by = (a: Analysis) => Object.fromEntries((a.trend?.annual ?? []).map((r) => [r.key, r]));
const years = (rows: (TrendRow | undefined)[]) => [...new Set(rows.flatMap((r) => r?.points.map((p) => p.fy) ?? []))].sort();
const valuesFor = (r: TrendRow | undefined, ys: string[], div = 1): (number | null)[] =>
  ys.map((y) => toNumber(r?.points.find((p) => p.fy === y)?.value) ?? null).map((v) => (v === null ? null : v / div));

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader title={title} subtitle={subtitle} />
      <div className="p-5">{children}</div>
    </Card>
  );
}

export default function FinancialsTab({
  report,
  resolved,
  onGoToReports,
}: {
  report: Report | null;
  resolved?: MissingAssessment["resolved"];
  onGoToReports: () => void;
}) {
  const a = report?.content.analysis;
  if (!report || !a?.available || !a.trend) {
    return (
      <div className="space-y-5">
        <EmptyState
          title={report ? "This report version has no financial analysis" : "Financial analysis appears once a report exists"}
          action={<button type="button" onClick={onGoToReports} className="rounded-[8px] border border-brand bg-brand px-4 py-2 text-sm font-medium text-white hover:bg-brand-hover">Go to Reports</button>}
        >
          {report ? "Generate a new report version to include trends, margins and returns." : "Charts and trends are built from a generated report. Generate one in the Reports tab."}
        </EmptyState>
        {resolved && resolved.length > 0 && (
          <Section title="Validated figures held today" subtitle="These are available now, before any report.">
            <ul className="grid gap-px overflow-hidden rounded-[10px] bg-line sm:grid-cols-2">
              {resolved.map((x) => (
                <li key={x.id} className="flex items-start justify-between gap-3 bg-surface px-4 py-3 text-[13px]">
                  <span><span className="block font-medium text-ink">{x.label}</span><span className="text-xs text-muted">{[x.found.period_end, x.found.basis?.toLowerCase()].filter(Boolean).join(" · ")}</span></span>
                  <span className="tnum font-medium text-ink">{fmtValue(x.found.value, x.found.unit)}</span>
                </li>
              ))}
            </ul>
          </Section>
        )}
      </div>
    );
  }

  const r = by(a);
  const rows = a.trend.annual;
  const bank = a.sector?.code === "BANK";
  const rev = r.revenue ?? r.total_income;
  const incomeLabel = rev?.label ?? "Revenue";
  const ysIncome = years([rev, r.net_profit]);
  const ysBs = years([r.total_assets, r.total_equity]);
  const ysRet = years([r.roe, r.roa, r.roce]);
  const ysMar = years([r.ebitda_margin, r.ebit_margin, r.net_margin]);
  const ysCf = years([r.cfo, r.cfi, r.cff]);
  const peak = (ks: string[]) => ks.map((k) => r[k]).filter((x): x is TrendRow => !!x);

  const groups: [string, string[]][] = [
    ["Financial performance", bank ? ["total_income", "nii", "net_profit", "eps"] : ["revenue", "ebitda", "ebit", "net_profit", "eps"]],
    ["Balance sheet", bank ? ["total_assets", "total_equity", "advances", "deposits", "cd_ratio"] : ["total_assets", "total_equity", "debt_to_equity", "current_ratio"]],
    ["Profitability and returns", bank ? ["roe", "roa", "cost_to_income", "ppop_margin", "provisions_to_ppop"] : ["roe", "roce", "ebitda_margin", "ebit_margin", "net_margin"]],
    ["Cash flow", bank ? [] : ["cfo", "cfi", "cff", "fcf", "cfo_to_profit"]],
  ];
  const lim = report.content.limitations;
  const gaps = [...lim.missing_critical, ...lim.missing_important, ...lim.unavailable_calculations];
  const val = a.valuation as { snapshot?: Record<string, { value: string; unit: string | null; period_end: string } | null>; historical?: { rows?: { fy: string; pe: string; price_date: string }[] } } | undefined;

  return (
    <div className="space-y-6">
      <Notice tone="info">
        Figures are shown on the <strong>{(a.basis ?? "").toLowerCase()}</strong> basis. Hover a card for its evidence. Values that are not available are listed under “Not available”, never shown as zero.
      </Notice>

      {groups.map(([title, keys]) => {
        const items = peak(keys);
        if (!items.length) return null;
        return (
          <Section key={title} title={title} subtitle={a.sector?.label}>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{items.map((x) => <MetricCard key={x.key} row={x} />)}</div>
          </Section>
        );
      })}
      {bank && (
        <Notice tone="neutral" title="Cash flow">
          {a.sector?.notes?.[0] ?? "For a bank, operating cash flow is dominated by changes in deposits and advances, so industrial-company cash-flow measures are not meaningful and are not reported."}
        </Notice>
      )}

      <Card>
        <CardHeader title="Trends" subtitle="Annual figures from validated evidence. Only periods that exist are drawn." />
        <div className="grid gap-8 p-5 lg:grid-cols-2">
          {ysIncome.length >= 2 && (
            <BarChart title={`${incomeLabel} and net profit`} unit="₹ thousand crore" categories={ysIncome}
              series={[{ name: incomeLabel, values: valuesFor(rev, ysIncome, CR * 1000) }, { name: "Net profit", values: valuesFor(r.net_profit, ysIncome, CR * 1000) }].filter((s) => s.values.some((v) => v !== null)) as Series[]}
              source={`Basis: ${(rev?.basis ?? "").toLowerCase()} · each bar has an evidence id in the table below`} />
          )}
          {ysBs.length >= 2 && (
            <BarChart title="Total assets and shareholders' equity" unit="₹ thousand crore" categories={ysBs}
              series={[{ name: "Total assets", values: valuesFor(r.total_assets, ysBs, CR * 1000) }, { name: "Equity", values: valuesFor(r.total_equity, ysBs, CR * 1000) }]} />
          )}
          {ysRet.length >= 2 && (
            <LineChart title="Return ratios" unit="percent" categories={ysRet}
              series={(["roe", "roa", "roce"] as const).filter((k) => r[k]).map((k) => ({ name: r[k]!.label, values: valuesFor(r[k], ysRet) }))} />
          )}
          {ysMar.length >= 2 && (
            <LineChart title="Margins" unit="percent" categories={ysMar}
              series={(["ebitda_margin", "ebit_margin", "net_margin"] as const).filter((k) => r[k]).map((k) => ({ name: r[k]!.label, values: valuesFor(r[k], ysMar) }))} />
          )}
          {!bank && ysCf.length >= 2 && (
            <BarChart title="Cash flow by activity" unit="₹ thousand crore" categories={ysCf}
              series={(["cfo", "cfi", "cff"] as const).filter((k) => r[k]).map((k) => ({ name: r[k]!.label, values: valuesFor(r[k], ysCf, CR * 1000) }))} />
          )}
          {val?.historical?.rows && val.historical.rows.length >= 2 && (
            <LineChart title="P/E at fiscal year-end" unit="x" categories={val.historical.rows.map((x) => x.fy)}
              series={[{ name: "P/E", values: val.historical.rows.map((x) => Number(x.pe)) }]} source="Closing price near the year end divided by that year's basic EPS." />
          )}
        </div>
      </Card>

      {val?.snapshot && (
        <Section title="Valuation snapshot" subtitle="Market multiples with their dates. Peer and intrinsic valuation are in the report.">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            {(["price", "pe", "pb"] as const).map((k) => {
              const s = val.snapshot?.[k];
              return (
                <div key={k} className="rounded-[10px] border border-line p-4">
                  <p className="text-xs font-medium tracking-wide text-muted uppercase">{k === "price" ? "Share price" : k === "pe" ? "P/E" : "P/B"}</p>
                  <p className="tnum mt-1 text-xl font-semibold text-ink">{s ? fmtValue(s.value, s.unit) : "n/a"}</p>
                  <p className="text-xs text-muted">{s ? `as of ${s.period_end}` : "not available"}</p>
                </div>
              );
            })}
          </div>
        </Section>
      )}

      <Card>
        <CardHeader title="Annual history and evidence" subtitle="Every value with the evidence record behind it." />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-[13px]">
            <thead className="bg-surface-2 text-left text-xs text-muted">
              <tr>
                <th className="sticky left-0 bg-surface-2 px-4 py-2 font-medium">Measure (₹ crore)</th>
                {years(rows).map((y) => <th key={y} className="px-3 py-2 text-right font-medium">{y}</th>)}
                <th className="px-3 py-2 text-right font-medium">Latest change</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {rows.map((row) => {
                const ch = change(row);
                return (
                  <tr key={row.key}>
                    <th scope="row" className="sticky left-0 bg-surface px-4 py-2 text-left font-medium text-ink">
                      {row.label}
                      <span className="block text-[11px] font-normal text-faint">{(row.basis ?? "").toLowerCase()}</span>
                    </th>
                    {years(rows).map((y) => {
                      const p = row.points.find((q) => q.fy === y);
                      return <td key={y} className="tnum px-3 py-2 text-right text-ink" title={p ? provenance(p) : undefined}>{p ? fmtCompact(p.value, p.unit) : "–"}</td>;
                    })}
                    <td className={`tnum px-3 py-2 text-right font-medium ${ch?.tone ?? "text-muted"}`}>{ch?.text ?? "–"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {rows.some((x) => x.basis_note) && <p className="border-t border-line px-5 py-3 text-xs text-muted">{rows.filter((x) => x.basis_note).map((x) => `${x.label}: ${x.basis_note}.`).join(" ")}</p>}
      </Card>

      {gaps.length > 0 && (
        <Card>
          <CardHeader title="Not available" subtitle="Shown as missing, with the reason. Never estimated." />
          <ul className="divide-y divide-line">
            {gaps.map((g, i) => (
              <li key={i} className="flex flex-wrap items-start justify-between gap-3 px-5 py-3 text-[13px]">
                <span><span className="block font-medium text-ink">{g.label}</span><span className="text-xs text-muted">{g.reason ?? "No reason recorded."} Affects: {g.affects}.</span></span>
                <Badge tone={g.importance === "CRITICAL" ? "bad" : "warn"}>{titleCase(g.importance)}</Badge>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
