import { Suspense } from "react";

import { SkeletonCard } from "@/components/ui";
import CompanyWorkspace from "@/features/company/CompanyWorkspace";

// `params` is runtime data in this Next.js version: it is resolved inside a Suspense boundary so the
// page shell can render immediately.
async function Workspace({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <CompanyWorkspace companyId={id} />;
}

export default function CompanyPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense fallback={<SkeletonCard lines={4} />}>
      <Workspace params={params} />
    </Suspense>
  );
}
