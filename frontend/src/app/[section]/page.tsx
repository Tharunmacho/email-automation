import { notFound } from "next/navigation";

import Home from "../page";
import { isNavId, NAV_IDS } from "@/lib/nav";

export function generateStaticParams() {
  return NAV_IDS.filter((section) => section !== "overview").map((section) => ({ section }));
}

export default async function SectionPage({
  params,
}: {
  params: Promise<{ section: string }>;
}) {
  const { section } = await params;
  if (!isNavId(section) || section === "overview") notFound();
  return <Home />;
}
