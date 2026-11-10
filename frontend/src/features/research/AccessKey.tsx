"use client";

import { useState, useSyncExternalStore } from "react";

import { getApiKey, setApiKey, subscribeApiKey } from "@/services/research";

export function useApiKey(): string {
  return useSyncExternalStore(subscribeApiKey, getApiKey, () => "");
}

export default function AccessKey() {
  const key = useApiKey();
  const [draft, setDraft] = useState("");

  if (key) {
    return (
      <p className="text-xs text-zinc-500">
        Research access key is set for this tab.{" "}
        <button type="button" className="underline" onClick={() => setApiKey("")}>
          Clear
        </button>
      </p>
    );
  }
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        setApiKey(draft.trim());
        setDraft("");
      }}
    >
      <label className="text-sm">
        <span className="block text-xs uppercase tracking-wide text-zinc-500">
          Research access key
        </span>
        <input
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="mt-1 w-64 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
        />
      </label>
      <button
        type="submit"
        disabled={!draft.trim()}
        className="rounded-lg bg-zinc-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40 dark:bg-zinc-100 dark:text-zinc-900"
      >
        Use key
      </button>
      <p className="basis-full text-xs text-zinc-500">
        Set by whoever runs the server (RESEARCH_API_KEYS). It is kept only in this browser tab.
      </p>
    </form>
  );
}
