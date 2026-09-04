import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { ChatShell } from "../components/chat/ChatShell";
import { requestPublicHandoff, startPublicChat } from "../lib/public-chat-api";

vi.mock("../lib/public-chat-api", () => ({
  requestPublicHandoff: vi.fn(),
  sendPublicChatMessage: vi.fn(),
  startPublicChat: vi.fn(),
}));

vi.mock("../lib/sse", () => ({ connectChatEvents: vi.fn(async () => undefined) }));

it("shows queued offline state and explains email follow-up without promising live response", async () => {
  vi.mocked(startPublicChat).mockResolvedValue({
    session: {
      id: "session-1",
      state: "AI_ACTIVE",
      version: 1,
      customer_name: null,
      customer_email: null,
      created_at: "2026-08-28T00:00:00Z",
      messages: [],
    },
    credential: { token: "opaque-token", expires_at: "2026-08-28T01:00:00Z" },
  });
  vi.mocked(requestPublicHandoff).mockResolvedValue({ state: "QUEUED" });

  render(<ChatShell publicKey="public-acme" />);
  fireEvent.click(screen.getByRole("button", { name: "Start chat" }));
  await screen.findByRole("button", { name: "Request a person" });
  fireEvent.click(screen.getByRole("button", { name: "Request a person" }));

  expect(await screen.findByText("Your request is queued for a support person.")).toBeVisible();
  expect(screen.getByText(/follow up may arrive by email/i)).toBeVisible();
  expect(screen.getByText(/not promising a live response/i)).toBeVisible();
});
