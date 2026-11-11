// Small dependency-free SVG charts. Each takes real values only: a missing value is a gap, never zero.

import { useId } from "react";

const PALETTE = ["#2457c5", "#8aa8c8", "#16865c", "#d68a16"];

export type Series = { name: string; values: (number | null)[]; color?: string };

const nice = (max: number) => {
  if (max <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(max)));
  const m = max / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
};
const fmt = (n: number) =>
  Math.abs(n) >= 100
    ? n.toLocaleString("en-IN", { maximumFractionDigits: 0 })
    : Math.abs(n) >= 10
      ? n.toLocaleString("en-IN", { maximumFractionDigits: 1 })
      : n.toLocaleString("en-IN", { maximumFractionDigits: 2 });

function Legend({ series }: { series: Series[] }) {
  return (
    <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {series.map((s, i) => (
        <li key={s.name} className="flex items-center gap-1.5">
          <span aria-hidden className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color ?? PALETTE[i % 4] }} />
          {s.name}
        </li>
      ))}
    </ul>
  );
}

/** Grouped vertical bars over categories. */
export function BarChart({
  title,
  unit,
  categories,
  series,
  source,
}: {
  title: string;
  unit: string;
  categories: string[];
  series: Series[];
  source?: string;
}) {
  const id = useId();
  const W = 560, H = 220, L = 46, B = 26, T = 12, R = 8;
  const all = series.flatMap((s) => s.values).filter((v): v is number => v !== null);
  if (!all.length) return null;
  const hi = nice(Math.max(...all, 0));
  const lo = Math.min(...all, 0) < 0 ? -nice(-Math.min(...all)) : 0;
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const gw = (W - L - R) / categories.length;
  const bw = Math.min(26, (gw * 0.7) / series.length);
  const ticks = [lo, lo + (hi - lo) / 2, hi];
  return (
    <figure aria-labelledby={id} className="min-w-0">
      <figcaption id={id} className="text-sm font-semibold text-ink">
        {title} <span className="font-normal text-muted">· {unit}</span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, ${unit}`} className="mt-2 w-full">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="#e2e8f0" />
            <text x={L - 6} y={y(t) + 3} textAnchor="end" fontSize="10" fill="#64748b">
              {fmt(t)}
            </text>
          </g>
        ))}
        {categories.map((c, ci) => {
          const x0 = L + ci * gw + (gw - bw * series.length) / 2;
          return (
            <g key={`${ci}-${c}`}>
              {series.map((s, si) => {
                const v = s.values[ci];
                if (v === null || v === undefined) return null;
                const top = y(Math.max(v, 0)), bot = y(Math.min(v, 0));
                return (
                  <g key={s.name}>
                    <rect x={x0 + si * bw} y={top} width={bw - 2} height={Math.max(1, bot - top)} rx="2" fill={s.color ?? PALETTE[si % 4]}>
                      <title>{`${s.name}, ${c}: ${fmt(v)} ${unit}`}</title>
                    </rect>
                  </g>
                );
              })}
              <text x={L + ci * gw + gw / 2} y={H - 8} textAnchor="middle" fontSize="10.5" fill="#64748b">
                {c}
              </text>
            </g>
          );
        })}
      </svg>
      <Legend series={series} />
      {source && <p className="mt-1 text-xs text-faint">{source}</p>}
    </figure>
  );
}

/** Lines over categories; a null breaks the line (no zero is drawn). */
export function LineChart({
  title,
  unit,
  categories,
  series,
  source,
}: {
  title: string;
  unit: string;
  categories: string[];
  series: Series[];
  source?: string;
}) {
  const id = useId();
  const W = 560, H = 200, L = 46, B = 26, T = 12, R = 12;
  const all = series.flatMap((s) => s.values).filter((v): v is number => v !== null);
  if (all.length < 2) return null;
  const mn = Math.min(...all), mx = Math.max(...all);
  const pad = (mx - mn || Math.abs(mx) || 1) * 0.15;
  const lo = mn - pad, hi = mx + pad;
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const x = (i: number) => L + (categories.length === 1 ? 0 : (i / (categories.length - 1)) * (W - L - R));
  return (
    <figure aria-labelledby={id} className="min-w-0">
      <figcaption id={id} className="text-sm font-semibold text-ink">
        {title} <span className="font-normal text-muted">· {unit}</span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${title}, ${unit}`} className="mt-2 w-full">
        {[lo, (lo + hi) / 2, hi].map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="#e2e8f0" />
            <text x={L - 6} y={y(t) + 3} textAnchor="end" fontSize="10" fill="#64748b">{fmt(t)}</text>
          </g>
        ))}
        {categories.map((c, i) => (
          <text key={`${i}-${c}`} x={x(i)} y={H - 8} textAnchor="middle" fontSize="10.5" fill="#64748b">{c}</text>
        ))}
        {series.map((s, si) => {
          const color = s.color ?? PALETTE[si % 4];
          let d = "";
          let pen = false;
          s.values.forEach((v, i) => {
            if (v === null) { pen = false; return; }
            d += `${pen ? "L" : "M"}${x(i)},${y(v)} `;
            pen = true;
          });
          return (
            <g key={s.name}>
              <path d={d} fill="none" stroke={color} strokeWidth="2" />
              {s.values.map((v, i) =>
                v === null ? null : (
                  <circle key={i} cx={x(i)} cy={y(v)} r="3" fill={color}>
                    <title>{`${s.name}, ${categories[i]}: ${fmt(v)} ${unit}`}</title>
                  </circle>
                ),
              )}
            </g>
          );
        })}
      </svg>
      <Legend series={series} />
      {source && <p className="mt-1 text-xs text-faint">{source}</p>}
    </figure>
  );
}

/** Horizontal bars for a handful of comparable values; the highlighted bar is this company. */
export function HBarChart({
  title,
  unit,
  rows,
  source,
}: {
  title: string;
  unit: string;
  rows: { label: string; value: number; highlight?: boolean }[];
  source?: string;
}) {
  const id = useId();
  if (rows.length < 2) return null;
  const hi = nice(Math.max(...rows.map((r) => r.value), 0));
  return (
    <figure aria-labelledby={id} className="min-w-0">
      <figcaption id={id} className="text-sm font-semibold text-ink">
        {title} <span className="font-normal text-muted">· {unit}</span>
      </figcaption>
      <ul className="mt-2 space-y-1.5">
        {rows.map((r) => (
          <li key={r.label} className="flex items-center gap-2 text-xs">
            <span className="w-20 shrink-0 truncate text-muted" title={r.label}>{r.label}</span>
            <span className="h-4 flex-1 rounded-sm bg-surface-2">
              <span
                className="block h-4 rounded-sm"
                style={{ width: `${Math.max(2, (Math.max(r.value, 0) / hi) * 100)}%`, background: r.highlight ? "#2457c5" : "#b8c9e6" }}
              />
            </span>
            <span className="tnum w-14 shrink-0 text-right font-medium text-ink">{fmt(r.value)}</span>
          </li>
        ))}
      </ul>
      {source && <p className="mt-1 text-xs text-faint">{source}</p>}
    </figure>
  );
}

/** Radar of 0-100 pillar scores. Pillars without a score are left out (never drawn as zero). */
export function Radar({ axes, label }: { axes: { name: string; value: number }[]; label: string }) {
  const size = 300, c = size / 2, r = 90;
  if (axes.length < 3) {
    return <p className="text-xs text-muted">At least three scored pillars are needed for a chart.</p>;
  }
  const pt = (i: number, v: number, extra = 0): [number, number] => {
    const a = (Math.PI * 2 * i) / axes.length - Math.PI / 2;
    const rr = (r + extra) * (v / 100);
    return [c + Math.cos(a) * rr, c + Math.sin(a) * rr];
  };
  const poly = axes.map((a, i) => pt(i, a.value).join(",")).join(" ");
  return (
    <svg viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label} className="mx-auto h-auto w-full max-w-[300px]">
      {[25, 50, 75, 100].map((g) => (
        <polygon key={g} points={axes.map((_, i) => pt(i, g).join(",")).join(" ")} fill="none" stroke="#e2e8f0" />
      ))}
      {axes.map((a, i) => {
        const [lx, ly] = pt(i, 100, 24);
        const [ax, ay] = pt(i, 100);
        return (
          <g key={a.name}>
            <line x1={c} y1={c} x2={ax} y2={ay} stroke="#e2e8f0" />
            <text x={lx} y={ly} fontSize="10.5" fill="#64748b" textAnchor={lx < c - 4 ? "end" : lx > c + 4 ? "start" : "middle"} dominantBaseline="middle">
              {a.name}
            </text>
          </g>
        );
      })}
      <polygon points={poly} fill="#2457c5" fillOpacity="0.14" stroke="#2457c5" strokeWidth="2" />
      {axes.map((a, i) => (
        <circle key={a.name} cx={pt(i, a.value)[0]} cy={pt(i, a.value)[1]} r="3" fill="#2457c5">
          <title>{`${a.name}: ${a.value.toFixed(1)} / 100`}</title>
        </circle>
      ))}
    </svg>
  );
}
