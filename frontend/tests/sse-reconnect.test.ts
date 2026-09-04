import { afterEach, expect, it, vi } from "vitest";

import { connectChatEvents } from "../lib/sse";

afterEach(() => vi.unstubAllGlobals());

it("reconnects after a persisted segment and delivers only the missing segment", async () => {
  const controller = new AbortController();
  const calls: string[] = [];
  const encoder = new TextEncoder();
  const fetchMock = vi.fn(async (input: string) => {
    calls.push(input);
    const first = calls.length === 1;
    const frames = first
      ? [
          'id: 2:v:0\nevent: message.validated\ndata: {"sequence":2,"citations":[],"segment_count":2}\n\n',
          'id: 2:s:0\nevent: message.segment\ndata: {"sequence":2,"index":0,"text":"First sentence. "}\n\n',
        ]
      : [
          'id: 2:s:1\nevent: message.segment\ndata: {"sequence":2,"index":1,"text":"Second sentence."}\n\n',
        ];
    return new Response(
      new ReadableStream({
        start(streamController) {
          streamController.enqueue(encoder.encode(frames.join("")));
          streamController.close();
        },
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  const delivered: string[] = [];

  await connectChatEvents({
    sessionId: "session-1",
    token: "opaque-token",
    signal: controller.signal,
    onEvent: (event) => {
      if (event.event !== "message.segment") return;
      delivered.push(String(event.data.text));
      if (event.data.index === 1) controller.abort();
    },
  });

  expect(delivered).toEqual(["First sentence. ", "Second sentence."]);
  expect(calls[1]).toContain("after=2%3As%3A0");
});
