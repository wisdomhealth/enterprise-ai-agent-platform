import type { ReactNode } from "react";

import { requireStaffSession } from "../../lib/staff-session";

export default async function StaffLayout({ children }: Readonly<{ children: ReactNode }>) {
  const staff = await requireStaffSession();
  return (
    <main>
      <header>
        <p>Signed in as {staff.email}</p>
      </header>
      {children}
    </main>
  );
}
