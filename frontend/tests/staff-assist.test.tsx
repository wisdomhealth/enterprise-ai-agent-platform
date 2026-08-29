import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { StaffAssist } from "../components/support/StaffAssist";
import { searchStaffKnowledge } from "../lib/staff-api";

vi.mock("../lib/staff-api", () => ({ searchStaffKnowledge: vi.fn() }));

it("shows internal source details as read-only staff reference", async () => {
  vi.mocked(searchStaffKnowledge).mockResolvedValue({
    text: "The SLA is one business day.",
    citations: [
      {
        title: "Support policy",
        section: "Response times",
        page_number: 2,
        chunk_id: "chunk-1",
        document_version_id: "version-1",
        internal_drive_link: "https://drive.example/internal",
      },
    ],
  });
  render(<StaffAssist />);
  fireEvent.change(screen.getByLabelText("Ask the knowledge base"), {
    target: { value: "What is the SLA?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));

  expect(await screen.findByText("The SLA is one business day.")).toBeVisible();
  expect(screen.getByText("Chunk chunk-1")).toBeVisible();
  expect(screen.getByRole("link", { name: "Open internal source" })).toHaveAttribute(
    "href",
    "https://drive.example/internal",
  );
  expect(screen.queryByRole("button", { name: /send|reply/i })).not.toBeInTheDocument();
});
