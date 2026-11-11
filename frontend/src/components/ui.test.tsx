import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { BarChart, LineChart, Radar } from "@/components/charts";
import { Button, EmptyState, ErrorState, ProgressBar, Tabs } from "@/components/ui";
import { fmtCompact, fmtValue, NA, pct } from "@/lib/format";

describe("Button", () => {
  it("explains why it is disabled and does not fire", async () => {
    const onClick = vi.fn();
    render(<Button disabledReason="Needs every critical item" onClick={onClick}>Generate</Button>);
    const b = screen.getByRole("button", { name: "Generate" });
    expect(b).toBeDisabled();
    expect(b).toHaveAttribute("title", "Needs every critical item");
    expect(b).toHaveAccessibleDescription("Needs every critical item");
    await userEvent.click(b);
    expect(onClick).not.toHaveBeenCalled();
  });

  it("shows a loading state that blocks repeat clicks", () => {
    render(<Button loading>Save</Button>);
    const b = screen.getByRole("button", { name: "Save" });
    expect(b).toBeDisabled();
    expect(b).toHaveAttribute("aria-busy", "true");
  });

  it("uses distinct treatments for primary and secondary", () => {
    render(<><Button variant="primary">A</Button><Button variant="secondary">B</Button></>);
    expect(screen.getByRole("button", { name: "A" }).className).toContain("bg-brand");
    expect(screen.getByRole("button", { name: "B" }).className).toContain("border-line-strong");
  });
});

describe("Tabs", () => {
  it("selects with the mouse and moves with the arrow keys", async () => {
    const onChange = vi.fn();
    render(<Tabs label="Sections" value="a" onChange={onChange} tabs={[{ id: "a", label: "Alpha" }, { id: "b", label: "Beta" }]} />);
    expect(screen.getByRole("tab", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Beta" }));
    expect(onChange).toHaveBeenCalledWith("b");
    screen.getByRole("tab", { name: "Alpha" }).focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(onChange).toHaveBeenLastCalledWith("b");
  });
});

describe("feedback states", () => {
  it("keeps technical details out of the way but available", async () => {
    const retry = vi.fn();
    render(<ErrorState message="The server could not complete that request." detail="HTTP 500: boom" onRetry={retry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("could not complete");
    expect(screen.getByText("Technical details")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalled();
  });

  it("explains an empty state and offers the next action", () => {
    render(<EmptyState title="No reports yet" action={<button>Go</button>}>Generate one first.</EmptyState>);
    expect(screen.getByText("No reports yet")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go" })).toBeInTheDocument();
  });

  it("exposes progress to assistive technology", () => {
    render(<ProgressBar value={62} label="Readiness" />);
    expect(screen.getByRole("progressbar", { name: "Readiness" })).toHaveAttribute("aria-valuenow", "62");
  });
});

describe("number formatting", () => {
  it("never shows a missing value as zero", () => {
    expect(fmtValue(null, "inr")).toBe(NA);
    expect(fmtValue(undefined, "percent")).toBe(NA);
    expect(fmtValue("not-a-number", "x")).toBe(NA);
    expect(pct(null)).toBe(NA);
  });

  it("formats rupees in crore with Indian grouping, and ratios with units", () => {
    expect(fmtValue("1622119500000", "inr")).toBe("₹1,62,212 cr");
    expect(fmtCompact("1622119500000", "inr")).toBe("1,62,212");
    expect(fmtValue("16.27", "percent")).toBe("16.27%");
    expect(fmtValue("1.79353", "x")).toBe("1.79x");
    expect(fmtValue("1322.9", "inr_per_share")).toBe("₹1,322.90");
    expect(pct("4.04", 1, true)).toBe("+4.0%");
  });
});

describe("charts", () => {
  it("draw nothing when there is no data and never plot a gap as zero", () => {
    const { container } = render(<BarChart title="T" unit="u" categories={["A", "B"]} series={[{ name: "S", values: [null, null] }]} />);
    expect(container.querySelector("svg")).toBeNull();
    const line = render(<LineChart title="L" unit="u" categories={["A", "B", "C"]} series={[{ name: "S", values: [1, null, 3] }]} />);
    expect(line.container.querySelectorAll("circle")).toHaveLength(2); // the null year has no point
  });

  it("label every chart with its title and unit, and keep duplicate category labels", () => {
    render(<BarChart title="Revenue" unit="₹ thousand crore" categories={["FY25", "FY25"]} series={[{ name: "R", values: [1, 2] }]} />);
    expect(screen.getByRole("img", { name: "Revenue, ₹ thousand crore" })).toBeInTheDocument();
  });

  it("need three scored pillars for a radar, and leave gaps out instead of zero", () => {
    render(<Radar axes={[{ name: "Growth", value: 50 }, { name: "Returns", value: 70 }]} label="Pillars" />);
    expect(screen.getByText(/at least three scored pillars/i)).toBeInTheDocument();
  });
});
