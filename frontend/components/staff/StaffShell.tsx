"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import type { StaffSession } from "../../lib/staff-session";

const pages: ReadonlyArray<{ href: string; label: string; description: string; admin?: boolean }> = [
  { href: "/staff/email", label: "Email", description: "Review drafts and delivery work." },
  { href: "/staff/support", label: "Support", description: "Manage customer handoffs and replies." },
  { href: "/staff/admin", label: "Admin", description: "Monitor operations and manage the platform.", admin: true },
  { href: "/staff/admin/authorization", label: "Authorization", description: "Manage access, Google connections, and Gmail Push.", admin: true },
];

function pageFor(pathname: string) {
  if (pathname.startsWith("/staff/admin/authorization")) return pages[3];
  if (pathname.startsWith("/staff/admin")) return pages[2];
  if (pathname.startsWith("/staff/support")) return pages[1];
  return pages[0];
}

export function StaffShell({ staff, children }: { staff: StaffSession; children: ReactNode }) {
  const pathname = usePathname();
  const current = pageFor(pathname);
  const navigation = pages.filter((page) => !page.admin || staff.role === "ADMIN");

  const links = navigation.map((page) => {
    const active = page.href === "/staff/admin"
      ? pathname === page.href
      : pathname.startsWith(page.href);
    return (
      <Link key={page.href} href={page.href} aria-current={active ? "page" : undefined}>
        {page.label}
      </Link>
    );
  });

  return (
    <div className="staff-shell">
      <aside className="staff-sidebar">
        <Link className="staff-brand" href="/">
          <span aria-hidden="true">EA</span>
          <strong>Enterprise AI</strong>
        </Link>
        <nav aria-label="Staff navigation">{links}</nav>
      </aside>
      <div className="staff-workspace">
        <header className="staff-topbar">
          <details className="staff-mobile-nav">
            <summary>Menu</summary>
            <nav aria-label="Mobile staff navigation">{links}</nav>
          </details>
          <div className="staff-page-heading">
            <p className="staff-eyebrow">Operations workspace</p>
            <h1>{current.label}</h1>
            <p>{current.description}</p>
          </div>
          <details className="staff-user-menu">
            <summary>{staff.email}</summary>
            <div>
              <strong>{staff.role}</strong>
              <span>{staff.email}</span>
              <a href="/api/v1/auth/logout">Sign out</a>
            </div>
          </details>
        </header>
        <main className="staff-content">{children}</main>
      </div>
    </div>
  );
}
