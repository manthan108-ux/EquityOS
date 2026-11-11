"use client";

import { useState, useSyncExternalStore } from "react";

import { Badge, Button, Field, inputClass } from "@/components/ui";
import { getApiKey, setApiKey, subscribeApiKey } from "@/services/research";

export function useApiKey(): string {
  return useSyncExternalStore(subscribeApiKey, getApiKey, () => "");
}

export default function AccessKey() {
  const key = useApiKey();
  const [draft, setDraft] = useState("");

  if (key) {
    return (
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Badge tone="ok" dot>Access key set for this browser tab</Badge>
        <Button size="sm" onClick={() => setApiKey("")}>Clear key</Button>
      </div>
    );
  }
  return (
    <form
      className="flex flex-wrap items-end gap-3"
      onSubmit={(e) => {
        e.preventDefault();
        setApiKey(draft.trim());
        setDraft("");
      }}
    >
      <Field label="Research access key" hint="Set by whoever runs the server (RESEARCH_API_KEYS). It is kept only in this browser tab.">
        <input type="password" autoComplete="off" value={draft} onChange={(e) => setDraft(e.target.value)} className={`${inputClass} w-72`} />
      </Field>
      <Button type="submit" variant="primary" disabled={!draft.trim()}>Use key</Button>
    </form>
  );
}
