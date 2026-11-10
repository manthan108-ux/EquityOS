import ResearchWorkspace from "@/features/research/ResearchWorkspace";
import ScoreCard from "@/features/scoring/ScoreCard";
import type { CompanySummary } from "@/services/api";

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-zinc-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium">{value}</dd>
    </div>
  );
}

export default function CompanyConfirmation({
  company,
}: {
  company: CompanySummary;
}) {
  const nse = company.listings.find((l) => l.exchange === "NSE");
  const bse = company.listings.find((l) => l.exchange === "BSE");
  // Both listings of one company share an ISIN; show the distinct values rather than assume.
  const isins = [...new Set(company.listings.map((l) => l.isin))].join(", ");

  return (
    <>
      <section
        aria-label="Confirm company"
        className="mt-6 rounded-xl border border-zinc-200 bg-white p-6 dark:border-zinc-800 dark:bg-zinc-900"
      >
        <p className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
          Confirm company
        </p>
        <h2 className="mt-1 text-xl font-semibold">{company.legal_name}</h2>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
          <Field label="NSE symbol" value={nse?.symbol ?? "Not listed"} />
          <Field label="BSE code" value={bse?.security_code ?? "Not listed"} />
          <Field label="ISIN" value={isins || "Unavailable"} />
          <Field label="Sector" value={company.sector ?? "Not classified"} />
          <Field
            label="Industry"
            value={company.industry ?? "Not classified"}
          />
        </dl>
      </section>
      <ResearchWorkspace
        key={`research-${company.id}`}
        companyId={company.id}
        companyName={company.common_name}
      />
      <ScoreCard key={company.id} companyId={company.id} ticker={nse?.symbol} />
    </>
  );
}
