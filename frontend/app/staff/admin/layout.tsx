import type { ReactNode } from "react";
import { redirect } from "next/navigation";

import { requireStaffSession } from "../../../lib/staff-session";

export default async function AdminLayout({ children }: Readonly<{ children: ReactNode }>) {
  const staff = await requireStaffSession();
  if (staff.role !== "ADMIN") redirect("/");
  return children;
}
