import type { ReactNode } from "react";

import { requireApplicationAccess } from "../../../lib/application-access";

export default async function SupportLayout({ children }: Readonly<{ children: ReactNode }>) {
  await requireApplicationAccess("support");
  return children;
}
