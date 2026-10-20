import type { ReactNode } from "react";

import { StaffShell } from "../../components/staff/StaffShell";
import { requireStaffSession } from "../../lib/staff-session";

export default async function StaffLayout({ children }: Readonly<{ children: ReactNode }>) {
  const staff = await requireStaffSession();
  return <StaffShell staff={staff}>{children}</StaffShell>;
}
