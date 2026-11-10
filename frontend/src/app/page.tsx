import CompanySearch from "@/features/search/CompanySearch";
import ReadinessOverview from "@/features/research/ReadinessOverview";
import SessionHistory from "@/features/research/SessionHistory";
import SystemStatus from "@/features/health/SystemStatus";

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center gap-12 px-6 py-20">
      <div className="max-w-xl text-center">
        <h1 className="text-4xl font-semibold tracking-tight">EquityOS</h1>
        <p className="mt-3 text-lg text-zinc-600 dark:text-zinc-400">
          Evidence-driven AI equity research for NSE and BSE listed companies.
        </p>
      </div>
      <CompanySearch />
      <SessionHistory />
      <ReadinessOverview />
      <div className="mt-auto flex w-full flex-col items-center gap-4 pt-12">
        <SystemStatus />
        <p className="max-w-md text-center text-xs text-zinc-500">
          EquityOS produces research assistance, not investment advice or a promise of returns.
        </p>
      </div>
    </main>
  );
}
