import type { Metadata } from "next";

export const metadata: Metadata = { title: { absolute: "Wick & Wax · demo shop" } };

export default function ShopLayout({ children }: LayoutProps<"/shop">) {
  return children;
}
