import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  DEFAULT_PREFERENCES,
  continueWithAvailable,
  describeFetch,
  downloadReportPdf,
  getApiKey,
  setApiKey,
  startResearch,
  subscribeApiKey,
  updatePreferences,
  uploadDocuments,
} from "@/services/research";

const reply = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("the access key", () => {
  it("lives in sessionStorage and notifies subscribers", () => {
    const seen = vi.fn();
    const off = subscribeApiKey(seen);
    expect(getApiKey()).toBe("");
    setApiKey("abc");
    expect(getApiKey()).toBe("abc");
    expect(window.localStorage.getItem("equityos.apiKey")).toBeNull();
    setApiKey("");
    expect(getApiKey()).toBe("");
    expect(seen).toHaveBeenCalledTimes(2);
    off();
    setApiKey("x");
    expect(seen).toHaveBeenCalledTimes(2);
  });
});

describe("requests", () => {
  it("send the key as a header and never in the URL", async () => {
    setApiKey("secret-key");
    fetchMock.mockReturnValue(reply(201, { id: "s1" }));
    await startResearch("c1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).not.toContain("secret-key");
    expect(new Headers(init.headers).get("X-API-Key")).toBe("secret-key");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ company_id: "c1" });
  });

  it("send the chosen preferences when starting", async () => {
    setApiKey("k");
    fetchMock.mockReturnValue(reply(201, {}));
    await startResearch("c1", { ...DEFAULT_PREFERENCES, basis: "STANDALONE" });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).preferences.basis).toBe("STANDALONE");
  });

  it("change preferences with PUT", async () => {
    setApiKey("k");
    fetchMock.mockReturnValue(reply(200, {}));
    await updatePreferences("s1", DEFAULT_PREFERENCES);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/research/sessions/s1/preferences");
    expect(init.method).toBe("PUT");
  });

  it("upload several files as multipart with the licence class", async () => {
    setApiKey("k");
    fetchMock.mockReturnValue(reply(202, { uploads: [] }));
    const files = [new File(["a"], "a.pdf"), new File(["b"], "b.csv")];
    await uploadDocuments("s1", files, "PUBLIC_FILING");
    const init = fetchMock.mock.calls[0][1];
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).getAll("files")).toHaveLength(2);
    expect((init.body as FormData).get("licence")).toBe("PUBLIC_FILING");
    expect(new Headers(init.headers).has("Content-Type")).toBe(false); // the browser sets the boundary
  });

  it("turn an error response into an ApiError with the server's message", async () => {
    setApiKey("k");
    fetchMock.mockReturnValue(
      reply(409, { detail: { message: "report not allowed", reasons: ["a critical item is missing"] } }),
    );
    await expect(continueWithAvailable("s1")).rejects.toMatchObject({
      status: 409,
      message: "report not allowed",
    });
    fetchMock.mockReturnValue(reply(401, { detail: "the API key is not valid" }));
    const err = await continueWithAvailable("s1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe("the API key is not valid");
  });

  it("download a PDF with the key header", async () => {
    setApiKey("k");
    fetchMock.mockReturnValue(Promise.resolve(new Response(new Blob(["%PDF-"]), { status: 200 })));
    const blob = await downloadReportPdf("r1");
    expect(blob.size).toBeGreaterThan(0);
    expect(fetchMock.mock.calls[0][1].headers["X-API-Key"]).toBe("k");
    fetchMock.mockReturnValue(Promise.resolve(new Response("{}", { status: 404 })));
    await expect(downloadReportPdf("r1")).rejects.toMatchObject({ status: 404 });
  });
});

describe("describeFetch", () => {
  it("says how many documents were downloaded", () => {
    expect(describeFetch({ status: "FETCHED", website: "https://x.com", downloaded: 3 })).toBe(
      "Downloaded 3 new documents from the company's own website.",
    );
    expect(describeFetch({ status: "FETCHED", website: null, downloaded: 1 })).toContain("1 new document ");
  });
  it("explains why nothing was fetched, in plain words", () => {
    expect(describeFetch({ status: "NO_WEBSITE", website: null })).toMatch(/no company website/i);
    expect(describeFetch({ status: "SITE_UNREACHABLE", website: "x" })).toMatch(/not bypassed/i);
    expect(describeFetch({ status: "NOTHING_FOUND", website: "x" })).toMatch(/no downloadable/i);
  });
  it("shows nothing when no fetch was attempted or the status is unknown", () => {
    expect(describeFetch(null)).toBeNull();
    expect(describeFetch(undefined)).toBeNull();
    expect(describeFetch({ status: "SOMETHING_NEW", website: null })).toBeNull();
  });
});
