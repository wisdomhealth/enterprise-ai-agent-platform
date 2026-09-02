import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { DeliveryStatus } from "../components/email/DeliveryStatus";
import { reconcileEmailDelivery, type EmailDelivery } from "../lib/staff-api";

vi.mock("../lib/staff-api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/staff-api")>();
  return { ...actual, reconcileEmailDelivery: vi.fn() };
});

const delivery: EmailDelivery = {
  id: "intent-1",
  state: "DELIVERY_UNKNOWN",
  version: 3,
  deterministic_message_id: "<delivery-1@mail.invalid>",
  last_error_code: "GMAIL_RESPONSE_UNKNOWN",
  attempts: [
    {
      id: "attempt-1",
      attempt_number: 1,
      outcome: "UNKNOWN",
      error_code: "GMAIL_RESPONSE_UNKNOWN",
      started_at: "2026-09-01T08:10:00Z",
      completed_at: "2026-09-01T08:11:00Z",
    },
  ],
};

it("offers reconciliation but no send or retry action for unknown delivery", () => {
  render(<DeliveryStatus delivery={delivery} />);

  expect(screen.getByRole("button", { name: "Check Gmail delivery" })).toBeVisible();
  expect(screen.getByText(/may already have been sent/i)).toBeVisible();
  expect(screen.queryByRole("button", { name: /send|retry/i })).not.toBeInTheDocument();
  expect(screen.getByText(/Attempt 1: Unknown/)).toBeVisible();
});

it("reconciles the existing durable delivery intent", async () => {
  vi.mocked(reconcileEmailDelivery).mockResolvedValue({
    id: delivery.id,
    work_item_id: "email-1",
    state: "SENT",
    version: 4,
  });
  render(<DeliveryStatus delivery={delivery} />);

  fireEvent.click(screen.getByRole("button", { name: "Check Gmail delivery" }));

  expect(reconcileEmailDelivery).toHaveBeenCalledWith(delivery.id, delivery.version);
  expect(await screen.findByText("Sent")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Check Gmail delivery" })).not.toBeInTheDocument();
});
