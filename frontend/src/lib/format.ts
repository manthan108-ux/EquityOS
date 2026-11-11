// Number formatting for financial values. Unavailable values are never shown as zero.

const CRORE = 1e7;
const IN = "en-IN";

export const NA = "n/a";

const num = (v: string | number | null | undefined): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

export function crore(v: string | number | null | undefined, digits = 0): string {
  const n = num(v);
  if (n === null) return NA;
  return `₹${(n / CRORE).toLocaleString(IN, { minimumFractionDigits: digits, maximumFractionDigits: digits })} cr`;
}

/** Value + unit as stored by the backend (inr, inr_per_share, percent, x, ratio, shares). */
export function fmtValue(v: string | number | null | undefined, unit: string | null | undefined): string {
  const n = num(v);
  if (n === null) return NA;
  switch (unit) {
    case "inr":
      return crore(n, Math.abs(n) < 1e9 ? 2 : 0);
    case "inr_per_share":
      return `₹${n.toLocaleString(IN, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    case "percent":
      return `${n.toLocaleString(IN, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
    case "x":
    case "ratio":
      return `${n.toLocaleString(IN, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}x`;
    case "shares":
      return n.toLocaleString(IN, { maximumFractionDigits: 0 });
    default:
      return n.toLocaleString(IN, { maximumFractionDigits: 2 }) + (unit ? ` ${unit}` : "");
  }
}

/** A table figure without its unit suffix: crore amounts as whole numbers. */
export function fmtCompact(v: string | number | null | undefined, unit: string | null | undefined): string {
  const n = num(v);
  if (n === null) return NA;
  if (unit === "inr") return (n / CRORE).toLocaleString(IN, { maximumFractionDigits: 0 });
  return fmtValue(v, unit);
}

export function pct(v: string | number | null | undefined, digits = 1, signed = false): string {
  const n = num(v);
  if (n === null) return NA;
  const s = n.toLocaleString(IN, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  return `${signed && n > 0 ? "+" : ""}${s}%`;
}

export const score1 = (v: string | number | null | undefined): string => {
  const n = num(v);
  return n === null ? NA : n.toLocaleString(IN, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
};

export const toNumber = num;

export const dateShort = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso.slice(0, 10);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
};

export const titleCase = (s: string | null | undefined): string =>
  (s ?? "").replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
