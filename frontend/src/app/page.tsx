import SystemStatus from "@/features/health/SystemStatus";

export default function Home() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-10 px-6 py-24">
      <div className="max-w-xl text-center">
        <h1 className="text-4xl font-semibold tracking-tight">EquityOS</h1>
        <p className="mt-3 text-lg text-zinc-600 dark:text-zinc-400">
          Evidence-driven AI equity research for NSE and BSE listed companies.
        </p>
        <p className="mt-6 text-sm text-zinc-500">
          Company search arrives in Phase 3. This page currently verifies that the frontend,
          backend and database are connected.
        </p>
      </div>
      <SystemStatus />
      <p className="max-w-md text-center text-xs text-zinc-500">
        EquityOS produces research assistance, not investment advice or a promise of returns.
      </p>
    </main>
  );
}
