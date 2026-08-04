import type { Metadata } from "next";
import { Inter, Outfit, Poppins, Fira_Code } from "next/font/google";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

// Poppins has no variable cut on Google Fonts, so the weights the dashboard
// actually sets have to be named here. Anything not listed gets synthesised by
// the browser — which is why the dashboard rules use 400/500/600/700 only and
// no longer ask for the 450 and 650 that Inter's variable axis used to serve.
const poppins = Poppins({
  variable: "--font-poppins",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const outfit = Outfit({
  variable: "--font-outfit",
  subsets: ["latin"],
});

// 700 is here for the dashboard console's level tags, which ask for bold. With
// only 400/500 loaded the browser was smearing a synthetic bold onto a 0.64rem
// monospace — the one place on the screen where stroke weight has to stay even.
const firaCode = Fira_Code({
  variable: "--font-fira-code",
  subsets: ["latin"],
  weight: ["400", "500", "700"],
});

export const metadata: Metadata = {
  title: "Ingrechef AI Candidate Automation",
  description: "Real-time candidate ingestion statistics & candidate directory.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${outfit.variable} ${poppins.variable} ${firaCode.variable} h-full antialiased`}
    >
      <body className="min-h-full flex">{children}</body>
    </html>
  );
}
