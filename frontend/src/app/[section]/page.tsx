import { notFound } from "next/navigation";

import Home from "../page";
import { isNavId, NAV_IDS } from "@/lib/nav";

export function generateStaticParams() {
  return NAV_IDS.map((section) => ({ section }));
}

export default async function SectionPage({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = await params;
  if (!isNavId(section)) notFound();
  return <Home />;
}
