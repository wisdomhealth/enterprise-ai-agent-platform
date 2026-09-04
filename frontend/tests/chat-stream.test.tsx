import { act, fireEvent, render, screen } from "@testing-library/react";
import { vi } from "vitest";

import { ChatShell } from "../components/chat/ChatShell";

vi.mock("../lib/public-chat-api", () => ({
  startPublicChat: vi.fn().mockResolvedValue({
    session: {
      id: "session-1",
      state: "AI_ACTIVE",
      version: 1,
      customer_name: null,
      customer_email: null,
      created_at: "2026-08-27T00:00:00Z",
      messages: [],
    },
    credential: { token: "opaque-token", expires_at: "2026-08-27T01:00:00Z" },
  }),
}));

vi.mock("../lib/sse", () => ({
  connectChatEvents: vi.fn(async (_options: object) => {
    const options = _options as {
      onEvent: (event: { cursor: string; sequence: number; event: string; data: object }) => void;
    };
    options.onEvent({
      sequence: 2,
      cursor: "2:v:0",
      event: "message.validated",
      data: { sequence: 2, citations: [], segment_count: 1 },
    });
    options.onEvent({
      sequence: 2,
      cursor: "2:s:0",
      event: "message.segment",
      data: { sequence: 2, index: 0, text: "Validated " },
    });
    options.onEvent({
      sequence: 2,
      cursor: "2:s:1",
      event: "message.segment",
      data: { sequence: 2, index: 1, text: "answer." },
    });
  }),
}));

it("renders each recovered SSE sequence once", async () => {
  render(<ChatShell publicKey="public-acme" />);

  fireEvent.click(screen.getByRole("button", { name: "Start chat" }));
  await act(async () => undefined);

  expect(screen.getAllByText("Validated answer.")).toHaveLength(1);
});
