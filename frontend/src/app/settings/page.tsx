"use client";

import { Card, CardHeader, Notice, PageHeader } from "@/components/ui";
import { useHealth } from "@/features/health/useHealth";
import AccessKey from "@/features/research/AccessKey";
import { API_URL } from "@/services/api";
import { DEFAULT_PREFERENCES } from "@/services/research";

const Row = ({ k, v }: { k: string; v: string }) => (
  <div className="flex flex-wrap items-center justify-between gap-2 px-5 py-3 text-[13px]">
    <dt className="text-muted">{k}</dt>
    <dd className="font-medium text-ink">{v}</dd>
  </div>
);

export default function SettingsPage() {
  const h = useHealth();
  const d = DEFAULT_PREFERENCES;
  return (
    <div className="space-y-6">
      <PageHeader title="Settings" subtitle="Access, research defaults and system information." />

      <Card>
        <CardHeader title="Research access" subtitle="Research sessions, uploads and reports are protected by a key set by whoever runs the server (RESEARCH_API_KEYS)." />
        <div className="space-y-3 p-5">
          <AccessKey />
          <p className="text-xs text-muted">The key is kept only in this browser tab. Closing the tab forgets it.</p>
        </div>
      </Card>

      <Card>
        <CardHeader title="Research preferences" subtitle="Preferences belong to each research session, so they can differ between companies." />
        <dl className="divide-y divide-line">
          <Row k="Accounting basis (default)" v={d.basis === "CONSOLIDATED" ? "Consolidated" : "Standalone"} />
          <Row k="Reporting period (default)" v="Latest available quarter" />
          <Row k="Optional items (default)" v="Not requested" />
        </dl>
        <div className="p-5">
          <Notice tone="info">To change them for a company, open its Overview tab and edit Research preferences. EquityOS never switches the accounting basis for you: a figure missing on your chosen basis is reported as missing.</Notice>
        </div>
      </Card>

      <Card>
        <CardHeader title="System information" />
        <dl className="divide-y divide-line">
          <Row k="API address" v={API_URL} />
          <Row k="Backend" v={h.phase === "ok" || h.phase === "degraded" ? `${h.live.service} v${h.live.version}` : h.phase === "loading" ? "Checking…" : "Unreachable"} />
          <Row k="Database" v={h.phase === "ok" ? "Connected" : h.phase === "loading" ? "Checking…" : "Unavailable"} />
          <Row k="Spreadsheet upload format" v="/api/research/template" />
        </dl>
        {h.phase === "error" && <p className="border-t border-line px-5 py-3 text-xs text-bad">{h.message}. Is the backend running at {API_URL}?</p>}
      </Card>
    </div>
  );
}
