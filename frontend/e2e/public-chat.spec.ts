import { expect, test } from "@playwright/test";

import { installPublicChatJourney } from "./fixtures";

test("public chat exposes only persisted validated SSE content", async ({ page }) => {
  await installPublicChatJourney(page);
  await page.goto("/chat/task26-public-key");
  await page.getByRole("button", { name: "Start chat" }).click();
  await expect(page.getByText("Chat started. An AI assistant is ready to help.")).toBeVisible();
  await expect(page.getByText("Refunds take five business days.")).toBeVisible();
  await expect(page.getByText(/chunk_id|internal_drive_link|drive\.google/i)).toHaveCount(0);

  await page.getByRole("textbox", { name: "Message" }).fill("What is the refund policy?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("What is the refund policy?")).toBeVisible();
  await page.getByRole("button", { name: "Request a person" }).click();
  await expect(page.getByText("Your request is queued for a support person.")).toBeVisible();
  await expect(page.getByText(/not promising a live response/i)).toBeVisible();
});
