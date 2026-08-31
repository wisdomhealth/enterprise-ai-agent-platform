import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import StaffSupportPage from "../app/staff/support/page";
import { SupportQueue } from "../components/support/SupportQueue";
import {
  claimHandoff,
  getSupportConversation,
  listSupportQueue,
  type SupportConversation,
  type SupportHandoff,
} from "../lib/staff-api";

vi.mock("../lib/staff-api", () => ({
  claimHandoff: vi.fn(),
  getSupportConversation: vi.fn(),
  listSupportQueue: vi.fn(),
  replyToHandoff: vi.fn(),
  resumeAi: vi.fn(),
  searchStaffKnowledge: vi.fn(),
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

function conversation(handoff: SupportHandoff, body: string): SupportConversation {
  return {
    ...handoff,
    customer: { name: body, email: null },
    summary: "",
    tool_results: [],
    messages: [
      {
        sequence: 1,
        actor: "CUSTOMER",
        body,
        status: "PERSISTED",
        created_at: "2026-08-29T00:00:00Z",
        citations: [],
      },
    ],
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listSupportQueue).mockResolvedValue([queuedHandoff]);
});

it("refreshes current state after a claim conflict", async () => {
  vi.mocked(claimHandoff).mockRejectedValue({
    status: 409,
    handoff: { ...queuedHandoff, state: "HUMAN_ACTIVE", assigned_user_id: "staff-2", version: 4 },
  });

  render(<SupportQueue />);
  fireEvent.click((await screen.findAllByRole("button", { name: "Claim" }))[0]);

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

it("keeps the conflicted handoff identity when another queue item exists", async () => {
  const other = { ...queuedHandoff, id: "handoff-2", session_id: "session-2", version: 7 };
  vi.mocked(listSupportQueue).mockResolvedValue([queuedHandoff, other]);
  vi.mocked(claimHandoff).mockRejectedValue({
    status: 409,
    handoff: { id: "", session_id: "", state: "HUMAN_ACTIVE", version: 4 },
  });
  const selected = vi.fn();
  render(<SupportQueue onSelect={selected} />);
  fireEvent.click((await screen.findAllByRole("button", { name: "Claim" }))[0]);

  expect(await screen.findByText("Already claimed")).toBeVisible();
  expect(selected).toHaveBeenCalledWith(
    expect.objectContaining({ id: "handoff-1", session_id: "session-1", version: 4 }),
  );
  expect(screen.getByRole("button", { name: "Conversation session-1" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Conversation session-2" })).toBeVisible();
  expect(screen.getByText("Version 7")).toBeVisible();
});

it.each([
  {
    outcome: "success",
    result: {
      ...queuedHandoff,
      state: "HUMAN_ACTIVE" as const,
      assigned_user_id: "staff-1",
      version: 4,
    },
  },
  {
    outcome: "conflict",
    result: {
      status: 409,
      handoff: { state: "HUMAN_ACTIVE" as const, version: 4 },
    },
  },
])("restores focus to the operated queue item after a claim $outcome", async ({ outcome, result }) => {
  const other = { ...queuedHandoff, id: "handoff-2", session_id: "session-2", version: 7 };
  vi.mocked(listSupportQueue).mockResolvedValue([queuedHandoff, other]);
  if (outcome === "success") {
    vi.mocked(claimHandoff).mockResolvedValue(result as unknown as SupportHandoff);
  } else {
    vi.mocked(claimHandoff).mockRejectedValue(result);
  }
  render(<SupportQueue />);
  const claims = await screen.findAllByRole("button", { name: "Claim" });
  claims[0].focus();
  fireEvent.click(claims[0]);

  await screen.findByText(outcome === "success" ? "Conversation claimed." : "Already claimed");
  expect(screen.getByRole("button", { name: "Conversation session-1" })).toHaveFocus();
  expect(screen.getByRole("button", { name: "Conversation session-2" })).not.toHaveFocus();
});

it("switches the transcript from A to B after B's claim conflict", async () => {
  const other = { ...queuedHandoff, id: "handoff-2", session_id: "session-2", version: 7 };
  vi.mocked(listSupportQueue).mockResolvedValue([queuedHandoff, other]);
  vi.mocked(claimHandoff).mockRejectedValue({
    status: 409,
    handoff: { state: "HUMAN_ACTIVE", version: 8 },
  });
  vi.mocked(getSupportConversation).mockImplementation(async (handoffId) => ({
    ...(handoffId === "handoff-1" ? queuedHandoff : other),
    customer: { name: handoffId === "handoff-1" ? "Customer A" : "Customer B", email: null },
    summary: "",
    tool_results: [],
    messages: [
      {
        sequence: 1,
        actor: "CUSTOMER",
        body: handoffId === "handoff-1" ? "Transcript A only" : "Transcript B only",
        status: "PERSISTED",
        created_at: "2026-08-29T00:00:00Z",
        citations: [],
      },
    ],
  }));

  render(<StaffSupportPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Conversation session-1" }));
  expect(await screen.findByText("Transcript A only")).toBeVisible();
  fireEvent.click((await screen.findAllByRole("button", { name: "Claim" }))[1]);

  expect(await screen.findByText("Transcript B only")).toBeVisible();
  expect(screen.queryByText("Transcript A only")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Conversation session-2" })).toHaveFocus();
});

it("keeps B selected when delayed A detail resolves after B's claim conflict", async () => {
  const other = { ...queuedHandoff, id: "handoff-2", session_id: "session-2", version: 7 };
  const a = deferred<SupportConversation>();
  const b = deferred<SupportConversation>();
  vi.mocked(listSupportQueue).mockResolvedValue([queuedHandoff, other]);
  vi.mocked(claimHandoff).mockRejectedValue({
    status: 409,
    handoff: { state: "HUMAN_ACTIVE", version: 8 },
  });
  vi.mocked(getSupportConversation).mockImplementation((handoffId) =>
    handoffId === "handoff-1" ? a.promise : b.promise,
  );

  render(<StaffSupportPage />);
  fireEvent.click(await screen.findByRole("button", { name: "Conversation session-1" }));
  fireEvent.click((await screen.findAllByRole("button", { name: "Claim" }))[1]);
  expect(await screen.findByText("Already claimed")).toBeVisible();
  await act(async () => b.resolve(conversation(other, "Transcript B wins")));
  expect(await screen.findByText("Transcript B wins")).toBeVisible();

  await act(async () => a.resolve(conversation(queuedHandoff, "Delayed transcript A")));

  expect(screen.getByText("Transcript B wins")).toBeVisible();
  expect(screen.queryByText("Delayed transcript A")).not.toBeInTheDocument();
});
