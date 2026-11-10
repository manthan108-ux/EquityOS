import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import AccessKey from "@/features/research/AccessKey";
import { getApiKey, setApiKey } from "@/services/research";

describe("AccessKey", () => {
  it("asks for a key and keeps it only in this tab's session storage", async () => {
    const user = userEvent.setup();
    render(<AccessKey />);
    expect(screen.getByRole("button", { name: /use key/i })).toBeDisabled();
    const input = screen.getByLabelText(/research access key/i);
    expect(input).toHaveAttribute("type", "password"); // never shown on screen
    await user.type(input, "  my-secret  ");
    await user.click(screen.getByRole("button", { name: /use key/i }));
    expect(getApiKey()).toBe("my-secret");
    expect(window.localStorage.length).toBe(0);
    expect(screen.queryByDisplayValue("my-secret")).not.toBeInTheDocument();
    expect(screen.getByText(/access key is set for this tab/i)).toBeInTheDocument();
  });

  it("can forget the key again", async () => {
    setApiKey("abc");
    const user = userEvent.setup();
    render(<AccessKey />);
    await user.click(screen.getByRole("button", { name: /clear/i }));
    expect(getApiKey()).toBe("");
    expect(screen.getByLabelText(/research access key/i)).toBeInTheDocument();
  });
});
