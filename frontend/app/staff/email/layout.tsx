import type { ReactNode } from "react";

import { requireApplicationAccess } from "../../../lib/application-access";

export default async function EmailLayout({ children }: Readonly<{ children: ReactNode }>) {
  await requireApplicationAccess("email");
  return children;
}
