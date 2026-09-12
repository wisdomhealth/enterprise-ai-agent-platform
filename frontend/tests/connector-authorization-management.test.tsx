import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ConnectorAuthorizationManagement } from "../components/admin/ConnectorAuthorizationManagement";

afterEach(cleanup);

const authorizations = [
  {
    staff_user_id: "staff-1",
    kind: "DRIVE" as const,
    resource_id: "drive-resource",
    authorize_endpoint: "/api/v1/admin/connectors/DRIVE/authorize",
    actions: [
      { action: "connector.create" as const, granted: false },
      { action: "connector.reauthorize" as const, granted: true },
      { action: "connector.revoke" as const, granted: false },
    ],
  },
  {
    staff_user_id: "staff-1",
    kind: "GMAIL" as const,
    resource_id: "gmail-resource",
    authorize_endpoint: "/api/v1/admin/connectors/GMAIL/authorize",
    actions: [
      { action: "connector.create" as const, granted: true },
      { action: "connector.reauthorize" as const, granted: true },
      { action: "connector.revoke" as const, granted: true },
    ],
  },
];

it("shows fixed connector actions and can grant the recommended action", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ConnectorAuthorizationManagement authorizations={authorizations} onSave={save} />);

  expect(screen.getByRole("heading", { name: "Connector authorization" })).toBeVisible();
  expect(screen.getByText("Google Drive")).toBeVisible();
  expect(screen.getByText("/api/v1/admin/connectors/DRIVE/authorize")).toBeVisible();
  expect(screen.getByText(/connector\.create · Missing/)).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Grant recommended Drive permissions" }));
  await waitFor(() =>
    expect(save).toHaveBeenCalledWith("DRIVE", [
      "connector.create",
      "connector.reauthorize",
      "connector.revoke",
    ]),
  );
});

it("revokes only the selected fixed action", async () => {
  const save = vi.fn().mockResolvedValue(undefined);
  render(<ConnectorAuthorizationManagement authorizations={authorizations} onSave={save} />);

  const gmail = screen.getByRole("group", { name: "Gmail connector permissions" });
  fireEvent.click(screen.getByRole("checkbox", { name: "Gmail connector.revoke" }));
  fireEvent.click(screen.getByRole("button", { name: "Save Gmail permissions" }));

  await waitFor(() =>
    expect(save).toHaveBeenCalledWith("GMAIL", ["connector.create", "connector.reauthorize"]),
  );
  expect(gmail).toBeVisible();
});
