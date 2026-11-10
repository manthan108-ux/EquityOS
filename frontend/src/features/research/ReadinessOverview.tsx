"use client";

import { useEffect, useState } from "react";

import { useApiKey } from "@/features/research/AccessKey";
import { fetchOverview, type Overview } from "@/services/research";

const BADGE: Record<string, string> = {
  AUTOMATIC: "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200",
  ASSISTED: "bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200",
  BLOCKED: "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-200",
};

/** Which companies can be researched automatically, and what to supply for the rest. */
export default function ReadinessOverview() {
  const key = useApiKey();
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!key) return;
    let live = true;
    fetchOverview()
      .then((d) => live && setData(d))
      .catch(() => live && setError("The readiness overview could not be loaded."));
    return () => {
      live = false;
    };
  }, [key]);

  if (!key) return null;
  if (error) return <p className="text-xs text-red-600">{error}</p>;
  if (!data) return null;
  return (
    <section aria-label="Research readiness overview" className="w-full max-w-4xl">
      <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500">
        Research readiness of every company
      </h2>
      <p className="mt-1 text-xs text-zinc-500">
        {data.automatic} ready for automatic research · {data.assisted} need documents · {data.blocked}{" "}
        have no usable data. This measures evidence, not how good a company is.
      </p>
      <div className="mt-2 overflow-x-auto rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase text-zinc-500">
            <tr>
              <th className="px-3 py-2">Company</th>
              <th className="px-3 py-2">Readiness</th>
              <th className="px-3 py-2">Critical items missing</th>
              <th className="px-3 py-2">Documents that would settle them</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
            {data.companies.map((c) => (
              <tr key={c.company_id} className="align-top">
                <td className="px-3 py-2">
                  <div className="font-medium">{c.name}</div>
                  <div className="text-xs text-zinc-500">
                    {c.symbol} · {c.profile}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className={`rounded-full px-2 py-0.5 text-xs ${BADGE[c.eligibility]}`}>
                    {c.eligibility.toLowerCase()}
                  </span>
                  <div className="mt-1 text-xs text-zinc-500">
                    {c.score} · confidence {c.confidence.toLowerCase()}
                  </div>
                </td>
                <td className="px-3 py-2 text-xs">
                  {c.critical_missing.length === 0 ? (
                    <span className="text-emerald-700 dark:text-emerald-300">none</span>
                  ) : (
                    c.critical_missing.map((m) => m.label).join(", ")
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-zinc-600 dark:text-zinc-400">
                  {c.documents_needed.length === 0
                    ? c.critical_missing.length
                      ? "nothing an upload can settle"
                      : "—"
                    : c.documents_needed.join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
