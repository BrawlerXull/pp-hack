import type { Metadata } from "next";
import { GuardShell } from "@/components/guard/Shell";

export const metadata: Metadata = { title: "War Room" };

// The dashboard is a client-rendered app behind a login, so there is no meaningful static shell
// to prefetch for instant navigation.
export const instant = false;

export default function GuardLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <GuardShell>{children}</GuardShell>;
}
