"use client";

import { useMemo } from "react";

import { BarChart } from "@/components/charts";
import { Badge, Card, Notice, type Tone } from "@/components/ui";
import { fmtValue, titleCase, toNumber } from "@/lib/format";
import type { Analysis, Report, ReportGap, ReportItem, ReportSection, Verdict } from "@/services/research";

const VTONE: Record<Verdict["verdict"], Tone> = { BUY: "ok", SELL: "bad", WATCH: "warn" };
const KIND_TONE: Record<string, Tone> = { "VERIFIED FACT": "ok", FACT: "ok", DERIVED: "info", INTERPRETATION: "warn", "ANALYTICAL HYPOTHESIS": "warn", "MANAGEMENT STATEMENT": "brand", "VERIFIED FACT: classification": "ok" };

const sec = (r: Report, key: string): ReportSection | undefined => r.content.sections.find((s) => s.key === key);
const ids = (xs: number[]) => (xs.length ? `evidence ${xs.slice(0, 4).map((i) => `#${i}`).join(", ")}` : "");

function Heading({ id, n, title, note }: { id: string; n: number; title: string; note?: string }) {
  return (
    <div id={id} className="scroll-mt-24 border-b border-line pb-2">
      <h2 className="text-lg font-semibold text-ink"><span className="mr-2 text-faint">{n}.</span>{title}</h2>
      {note && <p className="mt-0.5 text-xs text-muted">{note}</p>}
    </div>
  );
}

function Items({ items, gaps }: { items: ReportItem[]; gaps?: ReportGap[] }) {
  if (!items.length && !gaps?.length) return null;
  return (
    <div className="mt-3 overflow-x-auto rounded-[10px] border border-line">
      <table className="w-full min-w-[560px] text-[13px]">
        <thead className="bg-surface-2 text-left text-xs text-muted">
          <tr><th className="px-4 py-2 font-medium">Metric</th><th className="px-3 py-2 text-right font-medium">Value</th><th className="px-3 py-2 font-medium">Period / basis</th><th className="px-3 py-2 font-medium">Evidence</th></tr>
        </thead>
        <tbody className="divide-y divide-line">
          {items.map((it) => (
            <tr key={it.requirement + (it.period_end ?? "")}>
              <th scope="row" className="px-4 py-2 text-left font-medium text-ink">{it.label}</th>
              <td className="tnum px-3 py-2 text-right text-ink">{fmtValue(it.value, it.unit)}</td>
              <td className="px-3 py-2 text-muted">{[it.period_end, it.basis?.toLowerCase()].filter(Boolean).join(" · ")}</td>
              <td className="px-3 py-2 text-xs text-muted">{[it.source.page ? `p.${it.source.page}` : null, it.source.evidence_id ? `#${it.source.evidence_id}` : null].filter(Boolean).join(" · ")}</td>
            </tr>
          ))}
          {gaps?.map((g) => (
            <tr key={g.requirement} className="bg-warn-soft/50">
              <th scope="row" className="px-4 py-2 text-left font-medium text-ink">{g.label}</th>
              <td colSpan={3} className="px-3 py-2 text-xs text-muted">Not available. {g.reason} <span className="text-faint">Affects: {g.affects}.</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Points({ title, points }: { title: string; points: { text: string; kind: string; evidence_ids: number[] }[] }) {
  return (
    <div>
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      {points.length === 0 ? <p className="mt-1 text-[13px] text-muted">None established from the evidence.</p> : (
        <ul className="mt-2 space-y-2">
          {points.map((p, i) => (
            <li key={i} className="text-[13px] text-ink">
              {p.text} <Badge tone={KIND_TONE[p.kind] ?? "neutral"}>{titleCase(p.kind)}</Badge>{" "}
              <span className="text-xs text-faint">{ids(p.evidence_ids)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function VerdictBlock({ v }: { v: Verdict }) {
  const lv = v.levels as Record<string, string | undefined>;
  return (
    <div className="mt-3 rounded-[12px] border border-line bg-surface-2 p-5" aria-label="EquityOS verdict">
      <div className="flex flex-wrap items-center gap-4">
        <Badge tone={VTONE[v.verdict]} className="px-4 py-1.5 text-lg font-semibold">{v.verdict}</Badge>
        <div>
          <p className="text-sm font-medium text-ink">Evidence confidence: {titleCase(v.confidence.band)} ({v.confidence.points} / 100)</p>
          <p className="text-xs text-muted">Net points {v.net_points > 0 ? "+" : ""}{v.net_points}. BUY at +3 or more, SELL at −3 or less.</p>
        </div>
      </div>
      {lv.target ? (
        <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
          {[["Reference price", `₹${lv.price}`], [v.verdict === "BUY" ? "Target" : "Downside target", `₹${lv.target} (${lv.target_pct}%)`], [v.verdict === "BUY" ? "Stop-loss" : "Invalidation level", `₹${lv.stop_loss} (${lv.stop_pct}%)`], ["Reward-to-risk", lv.reward_to_risk ?? "n/a"]].map(([k, val]) => (
            <div key={k}><dt className="text-xs font-medium tracking-wide text-muted uppercase">{k}</dt><dd className="tnum text-base font-semibold text-ink">{val}</dd></div>
          ))}
        </dl>
      ) : (
        <p className="mt-3 text-[13px] text-ink">No trade levels are given: the verdict is WATCH.{lv.would_turn_buy_below ? ` With the other components unchanged, the valuation component alone would turn positive at ₹${lv.would_turn_buy_below} or below and negative at ₹${lv.would_turn_sell_above} or above.` : ""}</p>
      )}
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[480px] text-[13px]">
          <thead className="text-left text-xs text-muted"><tr><th className="py-1 font-medium">Component</th><th className="py-1 font-medium">Reading</th><th className="py-1 text-right font-medium">Points</th></tr></thead>
          <tbody className="divide-y divide-line">
            {v.components.map((c) => (
              <tr key={c.key}><th scope="row" className="py-1.5 pr-3 text-left font-medium text-ink">{c.label}{c.weight > 1 ? ` (×${c.weight})` : ""}</th><td className="py-1.5 pr-3 text-muted">{c.reading}</td><td className="tnum py-1.5 text-right font-medium text-ink">{c.available ? (c.points! > 0 ? `+${c.points}` : c.points) : "n/a"}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      {v.reasons.map((r) => <p key={r} className="mt-2 text-xs text-muted">Why not a trade: {r}.</p>)}
      <p className="mt-3 text-xs text-muted">{v.disclosure}</p>
    </div>
  );
}

type Scen = Record<string, { value_per_share?: string; vs_price_pct?: string; justified_pb?: string; peer_pe?: string; roe?: string }>;

function ValuationBlock({ a }: { a: Analysis }) {
  const v = a.valuation as { intrinsic?: { completed?: boolean; scenarios?: Scen; method?: string; reason?: string }; relative?: { completed?: boolean; scenarios?: Scen; method?: string; reason?: string }; assumption_note?: string; not_completed?: string[] } | undefined;
  if (!v) return null;
  const table = (title: string, s: Scen | undefined, extra: (x: Scen[string]) => string) =>
    s ? (
      <div key={title} className="mt-3">
        <h3 className="text-sm font-semibold text-ink">{title}</h3>
        <table className="mt-2 w-full max-w-xl text-[13px]">
          <thead className="text-left text-xs text-muted"><tr><th className="py-1 font-medium">Scenario</th><th className="py-1 font-medium">Basis</th><th className="py-1 text-right font-medium">Value / share</th><th className="py-1 text-right font-medium">vs price</th></tr></thead>
          <tbody className="divide-y divide-line">
            {(["bear", "base", "bull"] as const).map((k) => s[k] && (
              <tr key={k}><th scope="row" className="py-1.5 text-left font-medium text-ink">{titleCase(k)}</th><td className="py-1.5 text-muted">{extra(s[k])}</td><td className="tnum py-1.5 text-right text-ink">₹{s[k].value_per_share ?? "n/a"}</td><td className="tnum py-1.5 text-right text-muted">{s[k].vs_price_pct ?? "–"}%</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    ) : null;
  return (
    <div>
      {v.intrinsic?.completed ? table("Residual income (justified P/B)", v.intrinsic.scenarios, (x) => `ROE ${x.roe ? Number(x.roe).toFixed(1) : "–"}%, justified P/B ${x.justified_pb ?? "–"}x`) : v.intrinsic?.reason ? <p className="mt-3 text-[13px] text-muted">Intrinsic valuation not completed: {v.intrinsic.reason}.</p> : null}
      {v.relative?.completed ? table("Relative (peer P/E)", v.relative.scenarios, (x) => `peer P/E ${x.peer_pe ?? "–"}x`) : v.relative?.reason ? <p className="mt-3 text-[13px] text-muted">Relative valuation not completed: {v.relative.reason}.</p> : null}
      {v.assumption_note && <Notice tone="warn" title="Assumptions">{v.assumption_note}</Notice>}
      {v.not_completed && v.not_completed.length > 0 && <ul className="mt-3 list-disc pl-5 text-xs text-muted">{v.not_completed.map((n) => <li key={n}>{n}</li>)}</ul>}
    </div>
  );
}

export default function ReportViewer({ report }: { report: Report }) {
  const c = report.content;
  const a = c.analysis?.available ? c.analysis : undefined;
  const prefs = c.meta.preferences;
  const score = sec(report, "scorecard")?.score;
  const hist = sec(report, "historical")?.series;

  const toc = useMemo(() => {
    const t: [string, string][] = [["cover", "Cover"], ["summary", "Executive summary"], ["business", "Business overview"], ["financials", "Financial performance"], ["history", "Historical analysis"]];
    if (a?.peers?.rows.length) t.push(["peers", "Peer benchmarking"]);
    if (a?.valuation) t.push(["valuation", "Valuation"]);
    if (a?.management?.available) t.push(["management", "Management commentary"]);
    if (a?.thesis) t.push(["thesis", "Thesis, risks and catalysts"]);
    if (a?.verdict) t.push(["verdict", "Verdict"]);
    t.push(["scorecard", "Scorecard"]);
    if (a?.thesis) t.push(["conclusion", "Research conclusion"]);
    t.push(["register", "Evidence register"], ["limits", "Limitations"]);
    return t;
  }, [a]);

  let n = 0;
  const next = () => ++n;
  const register = c.sections.flatMap((s) => s.items.map((i) => ({ ...i, section: s.title })));
  const seen = new Set<string>();
  const uniqueRegister = register.filter((i) => { const k = `${i.requirement}-${i.source.evidence_id}`; if (seen.has(k)) return false; seen.add(k); return true; });
  const absent = [!a ? "Analysis, peer benchmarking, valuation, thesis and verdict (this version predates the analytical layer: generate a new version)" : null, a && !a.management?.available ? "Management commentary (no management-discussion section is stored)" : null].filter(Boolean) as string[];

  return (
    <div className="grid gap-8 lg:grid-cols-[200px_1fr]">
      <nav aria-label="Report sections" className="hidden lg:block">
        <ul className="sticky top-20 space-y-0.5 border-l border-line text-[13px]">
          {toc.map(([id, label]) => (
            <li key={id}><a href={`#${id}`} className="-ml-px block border-l-2 border-transparent py-1 pl-3 text-muted hover:border-brand hover:text-brand">{label}</a></li>
          ))}
        </ul>
      </nav>

      <article className="min-w-0 space-y-9">
        <section id="cover" className="scroll-mt-24">
          <Card className="overflow-hidden">
            <div className="bg-brand px-6 py-6 text-white">
              <p className="text-xs font-semibold tracking-widest uppercase opacity-80">Equity research</p>
              <h2 className="mt-1 text-3xl font-semibold">{c.meta.company}</h2>
              <p className="mt-1 text-sm opacity-90">{a?.sector?.label ?? ""}</p>
            </div>
            <dl className="grid grid-cols-2 gap-5 p-5 sm:grid-cols-4">
              {[["Report", `v${report.version} · ${report.mode === "COMPLETE" ? "Complete" : "Available evidence"}`], ["Generated", c.meta.as_of], ["Accounting basis", titleCase(prefs?.basis ?? "")], ["Reporting period", prefs?.period_label ?? "latest available"], ["Research readiness", `${c.readiness.score} / 100`], ["Evidence confidence", titleCase(c.readiness.confidence)], ["Financial score", score?.available ? `${Number(score.overall).toFixed(1)} (${titleCase(score.methodology)})` : "Not available"], ["Verdict", a?.verdict ? a.verdict.verdict : "Not in this version"]].map(([k, v]) => (
                <div key={k}><dt className="text-xs font-medium tracking-wide text-muted uppercase">{k}</dt><dd className="tnum mt-0.5 text-sm font-semibold text-ink">{v}</dd></div>
              ))}
            </dl>
            <p className="border-t border-line px-5 py-3 text-xs text-muted">{c.meta.banner} Readiness measures the evidence, not the company.</p>
          </Card>
        </section>

        <section className="space-y-3"><Heading id="summary" n={next()} title="Executive summary" />
          {a?.verdict && <VerdictBlock v={a.verdict} />}
          {a?.thesis && <p className="text-[13px] text-ink">Financial performance on the latest annual evidence is <strong>{a.thesis.trajectory.label}</strong> ({a.thesis.trajectory.basis}).</p>}
          {a?.trend && a.trend.observations.length > 0 && (
            <ul className="space-y-1.5">{a.trend.observations.slice(0, 6).map((o, i) => <li key={i} className="text-[13px] text-ink">{o.text} <Badge tone={KIND_TONE[o.kind] ?? "neutral"}>{titleCase(o.kind)}</Badge></li>)}</ul>
          )}
          {!a && <Notice tone="info">This report version contains the evidence register only. Generate a new version for the analytical sections.</Notice>}
        </section>

        <section className="space-y-3"><Heading id="business" n={next()} title="Business overview" />
          {a?.sector ? <p className="text-[13px] text-ink">{a.sector.lens}</p> : <p className="text-[13px] text-muted">The sector profile is not part of this version.</p>}
          <p className="text-xs text-muted">Business model, product mix, retail versus corporate exposure and competitive advantages need documents that are not parsed into figures yet; they are not estimated here.</p>
          {sec(report, "segments")?.segments && sec(report, "segments")!.segments!.length > 0 && (
            <ul className="text-[13px] text-ink">{sec(report, "segments")!.segments!.map((x, i) => <li key={i}>{x.segment}: {x.metric} {fmtValue(x.value, x.unit)}</li>)}</ul>
          )}
        </section>

        <section className="space-y-3"><Heading id="financials" n={next()} title="Financial performance" />
          {(["financial_performance", "profitability", "balance_sheet", "sector_analysis", "cash_flow"] as const).map((k) => { const s = sec(report, k); return s && (s.items.length || s.gaps.length) ? <div key={k}><h3 className="mt-2 text-sm font-semibold text-ink">{s.title}</h3><Items items={s.items} gaps={s.gaps} /></div> : null; })}
        </section>

        <section className="space-y-3"><Heading id="history" n={next()} title="Historical analysis" />
          {a?.trend ? (
            <>
              <ul className="space-y-1.5">{a.trend.observations.map((o, i) => <li key={i} className="text-[13px] text-ink">{o.text} <span className="text-xs text-faint">{ids(o.evidence_ids)}</span></li>)}</ul>
              {a.trend.unusual.length > 0 && <Notice tone="warn" title="Unusual movements (cause not established)"><ul className="list-disc pl-5">{a.trend.unusual.map((u, i) => <li key={i}>{u.text}</li>)}</ul></Notice>}
              <p className="text-xs text-muted">{a.trend.rules}.</p>
            </>
          ) : hist ? (
            <BarChart title="Annual series" unit="₹ thousand crore" categories={(hist.revenue ?? []).map((p) => p.fiscal_year)} series={[{ name: "Revenue", values: (hist.revenue ?? []).map((p) => (toNumber(p.value) ?? 0) / 1e10) }, { name: "Net profit", values: (hist.net_profit ?? []).map((p) => (toNumber(p.value) ?? 0) / 1e10) }]} />
          ) : <p className="text-[13px] text-muted">No annual history is stored.</p>}
        </section>

        {a?.peers && a.peers.rows.length > 0 && (
          <section className="space-y-3"><Heading id="peers" n={next()} title="Peer benchmarking" note="Same period, basis and unit; the full table is in the Peer comparison tab." />
            <p className="text-[13px] text-ink">Compared with {a.peers.peers.map((p) => p.name).join(", ")}: better than the peer median on {a.peers.summary.better.join(", ") || "none"}; worse on {a.peers.summary.worse.join(", ") || "none"}; in line on {a.peers.summary.in_line.join(", ") || "none"} ({a.peers.summary.ranked_metrics} rankable metrics). <Badge tone="warn">Interpretation</Badge></p>
          </section>
        )}

        {a?.valuation && <section className="space-y-3"><Heading id="valuation" n={next()} title="Valuation" note="Scenario-implied values under stated assumptions; not target prices." />{sec(report, "valuation") && <Items items={sec(report, "valuation")!.items} gaps={sec(report, "valuation")!.gaps} />}<ValuationBlock a={a} /></section>}

        {a?.management?.available && (
          <section className="space-y-3"><Heading id="management" n={next()} title="Management commentary" note="Verbatim, short, cited. These are management's statements, not EquityOS findings." />
            {a.management.topics?.map((t) => (
              <div key={t.key}><h3 className="text-sm font-semibold text-ink">{t.title} <Badge tone="brand">Management statement</Badge>{t.forward_looking && <Badge tone="warn" className="ml-1">Forward-looking</Badge>}</h3>
                <ul className="mt-1 space-y-1.5">{t.items.map((i, k) => <li key={k} className="border-l-2 border-line pl-3 text-[13px] text-ink">“{i.text}” <span className="text-xs text-faint">p.{i.page}</span></li>)}</ul></div>
            ))}
            {a.management.comparison && <p className="text-xs text-muted">{a.management.comparison}</p>}
          </section>
        )}

        {a?.thesis && (
          <section className="space-y-4"><Heading id="thesis" n={next()} title="Thesis, risks and catalysts" />
            <div className="grid gap-6 md:grid-cols-2"><Points title="Bull case" points={a.thesis.bull} /><Points title="Bear case" points={a.thesis.bear} /></div>
            <Points title="Catalysts" points={a.thesis.catalysts} />
            <div><h3 className="text-sm font-semibold text-ink">Risks</h3>
              <ul className="mt-2 space-y-2 text-[13px] text-ink">
                {[...a.thesis.risks.company, ...a.thesis.risks.valuation, ...a.thesis.risks.data, ...a.thesis.risks.governance].map((p, i) => <li key={i}>{p.text} <span className="text-xs text-faint">{ids(p.evidence_ids)}</span></li>)}
                {a.thesis.risks.sector.map((s) => <li key={s.title}><strong>{s.title}.</strong> {s.text} <Badge tone="neutral">Sector template</Badge></li>)}
              </ul></div>
            <div><h3 className="text-sm font-semibold text-ink">Monitoring indicators</h3>
              <ul className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{a.thesis.monitoring.map((m) => <li key={m.key} className="rounded-[10px] border border-line p-3 text-[13px]"><span className="block text-xs text-muted">{m.label}</span><span className="tnum font-semibold text-ink">{m.available ? fmtValue(m.value, m.unit ?? null) : "not available"}</span>{m.available && <span className="block text-[11px] text-faint">{m.period_end}{m.peer_median ? ` · peer median ${fmtValue(m.peer_median, m.unit ?? null)}` : ""}</span>}</li>)}</ul></div>
          </section>
        )}

        {a?.verdict && <section className="space-y-3"><Heading id="verdict" n={next()} title="Verdict" note={a.verdict.rules} /><VerdictBlock v={a.verdict} /></section>}

        <section className="space-y-3"><Heading id="scorecard" n={next()} title="Scorecard" />
          {score?.available ? <p className="text-[13px] text-ink">{titleCase(score.methodology)} financial score <strong className="tnum">{Number(score.overall).toFixed(1)}</strong> / 100, confidence {titleCase(score.confidence)}. See the Scorecard tab for pillars and history.</p> : <p className="text-[13px] text-muted">{score?.reason ?? "No score is available."}</p>}
          <p className="text-xs text-muted">The score is separate from research readiness ({c.readiness.score} / 100).</p>
        </section>

        {a?.thesis && <section className="space-y-3"><Heading id="conclusion" n={next()} title="Research conclusion" />
          {a.thesis.conclusion.answers.map((q) => <div key={q.question}><h3 className="text-sm font-semibold text-ink">{q.question}</h3><p className="text-[13px] text-ink">{q.answer}</p></div>)}</section>}

        <section className="space-y-3"><Heading id="register" n={next()} title="Evidence register and methodology" />
          <Items items={uniqueRegister} />
          {a && <p className="text-xs text-muted">{[a.trend?.rules, a.peers?.rules].filter(Boolean).join(" · ")}</p>}
        </section>

        <section className="space-y-3"><Heading id="limits" n={next()} title="Missing information and limitations" />
          {[["Critical items not available", c.limitations.missing_critical], ["Important items not available", c.limitations.missing_important], ["Optional items not available", c.limitations.missing_optional], ["Calculations that could not be made", c.limitations.unavailable_calculations]].map(([t, rows]) => (rows as ReportGap[]).length > 0 && <div key={t as string}><h3 className="text-sm font-semibold text-ink">{t as string}</h3><ul className="mt-1 list-disc pl-5 text-[13px] text-muted">{(rows as ReportGap[]).map((g) => <li key={g.requirement}><span className="text-ink">{g.label}</span>{g.reason ? `: ${g.reason}` : ""}</li>)}</ul></div>)}
          {c.limitations.limits_on_conclusions.length > 0 && <ul className="list-disc pl-5 text-xs text-muted">{c.limitations.limits_on_conclusions.map((x) => <li key={x}>{x}</li>)}</ul>}
          {absent.length > 0 && <Notice tone="neutral" title="Not part of this report version"><ul className="list-disc pl-5">{absent.map((x) => <li key={x}>{x}</li>)}</ul></Notice>}
          <p className="text-xs text-faint">EquityOS produces research assistance, not investment advice or a promise of returns.</p>
        </section>
      </article>
    </div>
  );
}
