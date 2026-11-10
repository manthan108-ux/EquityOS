import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import PreferencesPanel, { preferenceProblem } from "@/features/research/PreferencesPanel";
import { DEFAULT_PREFERENCES, type Preferences } from "@/services/research";

const setup = (value: Preferences = DEFAULT_PREFERENCES, log = []) => {
  const onApply = vi.fn();
  render(<PreferencesPanel value={value} onApply={onApply} log={log} />);
  return { onApply, user: userEvent.setup() };
};

describe("preferenceProblem", () => {
  it("accepts the defaults", () => {
    expect(preferenceProblem(DEFAULT_PREFERENCES)).toBeNull();
  });
  it("asks for a financial year when a year or quarter is chosen", () => {
    expect(preferenceProblem({ ...DEFAULT_PREFERENCES, period_mode: "FISCAL_YEAR" })).toMatch(
      /financial year/i,
    );
    expect(
      preferenceProblem({ ...DEFAULT_PREFERENCES, period_mode: "FISCAL_YEAR", fiscal_year: 1800 }),
    ).toMatch(/financial year/i);
  });
  it("asks for a quarter when a quarter is chosen", () => {
    expect(
      preferenceProblem({ ...DEFAULT_PREFERENCES, period_mode: "QUARTER", fiscal_year: 2027 }),
    ).toMatch(/quarter/i);
    expect(
      preferenceProblem({
        ...DEFAULT_PREFERENCES,
        period_mode: "QUARTER",
        fiscal_year: 2027,
        quarter: "Q2",
      }),
    ).toBeNull();
  });
});

describe("PreferencesPanel", () => {
  it("shows the product defaults: consolidated, latest quarter, optional items off", () => {
    setup();
    expect(screen.getByLabelText(/consolidated \(default\)/i)).toBeChecked();
    expect(screen.getByLabelText(/standalone/i)).not.toBeChecked();
    expect(screen.getByLabelText(/reporting period/i)).toHaveValue("LATEST_QUARTER");
    expect(screen.getByLabelText(/optional items/i)).not.toBeChecked();
    expect(screen.getByRole("button", { name: /apply preferences/i })).toBeDisabled();
  });

  it("states that the basis is never switched silently", () => {
    setup();
    expect(screen.getByText(/never switched for you/i)).toBeInTheDocument();
  });

  it("applies a changed basis", async () => {
    const { onApply, user } = setup();
    await user.click(screen.getByLabelText(/standalone/i));
    await user.click(screen.getByRole("button", { name: /apply preferences/i }));
    expect(onApply).toHaveBeenCalledWith({ ...DEFAULT_PREFERENCES, basis: "STANDALONE" });
  });

  it("asks for the year before a chosen year can be applied", async () => {
    const { onApply, user } = setup();
    await user.selectOptions(screen.getByLabelText(/reporting period/i), "FISCAL_YEAR");
    expect(screen.getByRole("alert")).toHaveTextContent(/financial year/i);
    const apply = screen.getByRole("button", { name: /apply preferences/i });
    expect(apply).toBeDisabled();
    await user.type(screen.getByLabelText(/^financial year$/i), "2026");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(apply);
    expect(onApply).toHaveBeenCalledWith({
      ...DEFAULT_PREFERENCES,
      period_mode: "FISCAL_YEAR",
      fiscal_year: 2026,
      quarter: null,
    });
  });

  it("needs both a year and a quarter for a specific quarter", async () => {
    const { onApply, user } = setup();
    await user.selectOptions(screen.getByLabelText(/reporting period/i), "QUARTER");
    await user.type(screen.getByLabelText(/^financial year$/i), "2027");
    expect(screen.getByRole("alert")).toHaveTextContent(/quarter/i);
    await user.selectOptions(screen.getByLabelText(/^quarter$/i), "Q2");
    await user.click(screen.getByRole("button", { name: /apply preferences/i }));
    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ period_mode: "QUARTER", fiscal_year: 2027, quarter: "Q2" }),
    );
  });

  it("clears the year and quarter again when going back to a latest period", async () => {
    const { onApply, user } = setup({
      ...DEFAULT_PREFERENCES,
      period_mode: "QUARTER",
      fiscal_year: 2027,
      quarter: "Q1",
    });
    await user.selectOptions(screen.getByLabelText(/reporting period/i), "LATEST_ANNUAL");
    await user.click(screen.getByRole("button", { name: /apply preferences/i }));
    expect(onApply).toHaveBeenCalledWith(
      expect.objectContaining({ period_mode: "LATEST_ANNUAL", fiscal_year: null, quarter: null }),
    );
  });

  it("lets the user ask for optional items", async () => {
    const { onApply, user } = setup();
    await user.click(screen.getByLabelText(/optional items/i));
    await user.click(screen.getByRole("button", { name: /apply preferences/i }));
    expect(onApply).toHaveBeenCalledWith({ ...DEFAULT_PREFERENCES, include_optional: true });
  });

  it("lists the earlier choices of a saved session", () => {
    const log = [
      { at: "2026-11-10T10:00:00Z", by: "a", from: null, to: { basis: "CONSOLIDATED", period_mode: "FISCAL_YEAR", fiscal_year: 2026 } },
      { at: "2026-11-10T11:00:00Z", by: "a", from: {}, to: { basis: "STANDALONE", period_mode: "LATEST_QUARTER" } },
    ] as never[];
    setup(DEFAULT_PREFERENCES, log);
    expect(screen.getByText(/earlier choices in this session \(1\)/i)).toBeInTheDocument();
    expect(screen.getByText(/fiscal year FY2026/i)).toBeInTheDocument();
  });
});
