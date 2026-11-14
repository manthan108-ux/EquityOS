"use client";

import Link from "next/link";

import { Notice, PageHeader } from "@/components/ui";
import PipelineDashboard from "@/features/pipeline/PipelineDashboard";
import { useApiKey } from "@/features/research/AccessKey";

export default function PipelinePage() {
  const key = useApiKey();
  return (
    <div className="space-y-6">
      <PageHeader
        title="Data pipeline"
        subtitle="Every stage a company passes through, from industry label and website to documents, evidence and readiness, with what actually happened at each one."
      />
      {key ? (
        <PipelineDashboard />
      ) : (
        <Notice tone="warn" title="Access key needed">
          Set the access key in <Link href="/settings" className="font-medium text-brand underline">Settings</Link> to see the pipeline.
        </Notice>
      )}
    </div>
  );
}
