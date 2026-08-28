import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { SupportQueue } from "../components/support/SupportQueue";
import { claimHandoff, listSupportQueue } from "../lib/staff-api";

vi.mock("../lib/staff-api", () => ({
  claimHandoff: vi.fn(),
  listSupportQueue: vi.fn(),
}));

const queuedHandoff = {
  id: "handoff-1",
  session_id: "session-1",
  state: "QUEUED" as const,
  trigger: "CUSTOMER_REQUEST",
  assigned_user_id: null,
  version: 3,
  last_customer_sequence: 2,
};

beforeEach(() => {
  vi.mocked(listSupportQueue).mockResolvedValue([queuedHandoff]);
});

it("refreshes current state after a claim conflict", async () => {
  vi.mocked(claimHandoff).mockRejectedValue({
    status: 409,
    handoff: { ...queuedHandoff, state: "HUMAN_ACTIVE", assigned_user_id: "staff-2", version: 4 },
  });

  render(<SupportQueue />);
  fireEvent.click(await screen.findByRole("button", { name: "Claim" }));

  expect(await screen.findByText("Already claimed")).toBeVisible();
  expect(screen.getByText("Version 4")).toBeVisible();
  expect(screen.getByText("Human active")).toBeVisible();
});

it("preserves the claim control focus after a successful claim", async () => {
  vi.mocked(claimHandoff).mockResolvedValue({
    ...queuedHandoff,
    state: "HUMAN_ACTIVE",
    assigned_user_id: "staff-1",
    version: 4,
  });
  render(<SupportQueue />);
  const claim = await screen.findByRole("button", { name: "Claim" });
  claim.focus();
  fireEvent.click(claim);

  expect(await screen.findByText("Human active")).toBeVisible();
  expect(screen.getByRole("button", { name: "Conversation session-1" })).toHaveFocus();
});
