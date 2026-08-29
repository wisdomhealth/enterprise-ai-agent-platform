import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { ConversationPanel } from "../components/support/ConversationPanel";
import { resumeAi } from "../lib/staff-api";

vi.mock("../lib/staff-api", () => ({ resumeAi: vi.fn() }));

const humanActiveConversation = {
  id: "handoff-1",
  session_id: "session-1",
  state: "HUMAN_ACTIVE" as const,
  trigger: "CUSTOMER_REQUEST",
  assigned_user_id: "staff-1",
  version: 4,
  last_customer_sequence: 2,
  customer: { name: "Ada", email: "ada@example.test" },
  summary: "",
  tool_results: [],
  messages: [
    {
      sequence: 1,
      actor: "CUSTOMER" as const,
      body: "Please help",
      status: "PERSISTED" as const,
      created_at: "2026-08-28T00:00:00Z",
      citations: [],
    },
  ],
};

it("shows Resume AI only for a human-active claimed conversation", () => {
  render(<ConversationPanel conversation={humanActiveConversation} />);

  expect(screen.getByRole("button", { name: "Resume AI" })).toBeVisible();
});

it("requires explicit confirmation before resuming AI", async () => {
  vi.mocked(resumeAi).mockResolvedValue({ ...humanActiveConversation, state: "AI_ACTIVE", version: 5 });
  render(<ConversationPanel conversation={humanActiveConversation} />);
  fireEvent.click(screen.getByRole("button", { name: "Resume AI" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Resume AI");
  expect(resumeAi).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm resume AI" }));

  expect(resumeAi).toHaveBeenCalledWith("handoff-1", 4);
  expect(await screen.findByText("AI will wait for the next customer message.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Resume AI" })).not.toBeInTheDocument();
});

it("does not show Resume AI for an unclaimed or non-human conversation", () => {
  render(
    <ConversationPanel
      conversation={{ ...humanActiveConversation, state: "QUEUED", assigned_user_id: null }}
    />,
  );

  expect(screen.queryByRole("button", { name: "Resume AI" })).not.toBeInTheDocument();
});

it("renders the authorized transcript citation using the durable source schema", () => {
  render(
    <ConversationPanel
      conversation={{
        ...humanActiveConversation,
        messages: [
          {
            ...humanActiveConversation.messages[0],
            actor: "AI",
            citations: [
              {
                chunk_id: "chunk-1",
                document_version_id: "version-1",
                title: "Support policy",
                section: "Response times",
                page_number: 2,
                internal_drive_link: "https://drive.google.com/internal-policy",
              },
            ],
          },
        ],
      }}
    />,
  );

  fireEvent.click(screen.getByText("Internal source: Support policy"));
  expect(screen.getByText("Version version-1")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open internal source" })).toHaveAttribute(
    "href",
    "https://drive.google.com/internal-policy",
  );
});
