import { render, screen } from "@testing-library/react";

import { ChatShell } from "../components/chat/ChatShell";

it("allows an anonymous customer to start without contact details", () => {
  render(<ChatShell publicKey="public-acme" />);

  expect(screen.getByRole("button", { name: "Start chat" })).toBeEnabled();
  expect(screen.queryByLabelText("Email")).not.toBeRequired();
});
