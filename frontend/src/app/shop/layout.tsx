import type { Metadata } from "next";

export const metadata: Metadata = { title: { absolute: "Wick & Wax · demo shop" } };

export default function ShopLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return children;
}
