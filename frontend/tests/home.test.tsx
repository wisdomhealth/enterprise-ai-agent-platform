import { render, screen } from "@testing-library/react";
import HomePage from "../app/page";

it("identifies the customer support platform", () => {
  render(<HomePage />);
  expect(screen.getByRole("heading", { name: "Enterprise AI Support" })).toBeVisible();
});

it("publishes exactly the six approved application and health destinations", () => {
  render(<HomePage />);

  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(6);
  expect(screen.getByRole("link", { name: /Email Assistant/ })).toHaveAttribute(
    "href",
    "/staff/email",
  );
  expect(screen.getByRole("link", { name: /Support Console/ })).toHaveAttribute(
    "href",
    "/staff/support",
  );
  expect(screen.getByRole("link", { name: /Admin Dashboard/ })).toHaveAttribute(
    "href",
    "/staff/admin",
  );
  expect(screen.getByRole("link", { name: /Authorization Management/ })).toHaveAttribute(
    "href",
    "/staff/admin/authorization",
  );
  expect(screen.getByRole("link", { name: /Liveness/ })).toHaveAttribute(
    "href",
    "http://localhost/health/live",
  );
  expect(screen.getByRole("link", { name: /Readiness/ })).toHaveAttribute(
    "href",
    "/health/ready",
  );
  expect(document.querySelector('a[href^="/chat/"]')).not.toBeInTheDocument();
  expect(document.querySelector('a[href^="/staff/email/"]')).not.toBeInTheDocument();
});

it("uses the existing relative Google OIDC login endpoint", () => {
  render(<HomePage />);

  const loginButton = screen.getByRole("button", { name: "Sign in" });
  expect(loginButton.closest("form")).toHaveAttribute("action", "/api/v1/auth/login");
  expect(loginButton.closest("form")).toHaveAttribute("method", "get");
});
