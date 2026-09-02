import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { expect, it, vi } from "vitest";

import { DeliveryStatus } from "../components/email/DeliveryStatus";
import {
  reconcileEmailDelivery,
  StaffApiError,
  type EmailDelivery,
  type EmailDetail,
} from "../lib/staff-api";
import { emailDetail } from "./fixtures/email";

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
  render(<DeliveryStatus delivery={delivery} onRefresh={vi.fn()} />);

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
  const refresh = vi.fn();
  render(<DeliveryStatus delivery={delivery} onRefresh={refresh} />);

  fireEvent.click(screen.getByRole("button", { name: "Check Gmail delivery" }));

  expect(reconcileEmailDelivery).toHaveBeenCalledWith(delivery.id, delivery.version);
  await waitFor(() => expect(refresh).toHaveBeenCalledOnce());
});

it("refreshes the complete durable detail after a delivery conflict", async () => {
  const latest: EmailDetail = {
    ...emailDetail,
    state: "SEND_RETRY_WAIT",
    version: 9,
    delivery: {
      ...delivery,
      state: "SEND_RETRY_WAIT",
      version: 7,
      attempts: [
        ...delivery.attempts,
        {
          id: "attempt-2",
          attempt_number: 2,
          outcome: "DEFINITIVE_FAILURE",
          error_code: "GMAIL_RATE_LIMITED",
          started_at: "2026-09-01T08:12:00Z",
          completed_at: "2026-09-01T08:13:00Z",
        },
      ],
    },
  };
  vi.mocked(reconcileEmailDelivery).mockRejectedValue(new StaffApiError("Changed", 409));
  const refresh = vi.fn(async () => latest);

  function Harness() {
    const [current, setCurrent] = useState<EmailDetail>({ ...emailDetail, delivery });
    const refreshAndReplace = async () => {
      const value = await refresh();
      setCurrent(value);
    };
    return <DeliveryStatus delivery={current.delivery!} onRefresh={refreshAndReplace} />;
  }

  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "Check Gmail delivery" }));

  expect(await screen.findByText("Delivery changed elsewhere. The latest state is shown.")).toBeVisible();
  expect(refresh).toHaveBeenCalledOnce();
  expect(screen.getByText(/Attempt 2: Definitive failure/)).toBeVisible();
  expect(screen.getByRole("button", { name: "Retry delivery" })).toBeVisible();
  expect(screen.queryByRole("button", { name: "Check Gmail delivery" })).not.toBeInTheDocument();
});
