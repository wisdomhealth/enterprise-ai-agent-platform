import { render, screen } from "@testing-library/react";
import HomePage from "../app/page";

it("identifies the customer support platform", () => {
  render(<HomePage />);
  expect(screen.getByRole("heading", { name: "Enterprise AI Support" })).toBeVisible();
});
