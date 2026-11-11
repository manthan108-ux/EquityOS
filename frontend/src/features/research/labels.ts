import type { Tone } from "@/components/ui";
import type { SessionStatus } from "@/services/research";

/** Session status in the user's words. */
export const SESSION_LABEL: Record<SessionStatus["status"], { label: string; tone: Tone }> = {
  COMPLETED: { label: "Report ready", tone: "ok" },
  AWAITING_DOCUMENTS: { label: "Waiting for documents", tone: "warn" },
  RUNNING: { label: "Running", tone: "info" },
  READY: { label: "Ready for report", tone: "brand" },
  FAILED: { label: "Needs retry", tone: "bad" },
  CREATED: { label: "Starting", tone: "info" },
};
