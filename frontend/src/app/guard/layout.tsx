import { GuardShell } from "@/components/guard/Shell";

// The dashboard is a client-rendered app behind a login, so there is no meaningful static shell
// to prefetch for instant navigation.
export const instant = false;

export default function GuardLayout({ children }: LayoutProps<"/guard">) {
  return <GuardShell>{children}</GuardShell>;
}
