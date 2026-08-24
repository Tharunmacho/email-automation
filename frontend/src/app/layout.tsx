import type { Metadata } from "next";
import { Outfit } from "next/font/google";
import "./globals.css";

// One family, product-wide. Outfit is a variable font on Google Fonts, so the
// whole 100–900 axis arrives in a single file and every weight the sheet asks
// for is a real cut rather than a synthetic smear. Nothing else is loaded: the
// places that used to reach for Inter, Poppins or a monospace now take this
// same face, with `tabular-nums` and letter-spacing doing the work that a
// second family used to.
const outfit = Outfit({
  variable: "--font-outfit",
  subsets: ["latin"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Adira",
  description: "Candidate sourcing, email extraction, and resume parsing in one workspace.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    // Light is stamped here so the very first paint is light — the theme module
    // only ever moves it to dark, and only when the pill has been pressed
    // before. Without this the document loads themeless and a browser set to
    // dark styles the scrollbars and form controls before hydration lands.
    <html
      lang="en"
      data-theme="light"
      style={{ colorScheme: "light" }}
      className={`${outfit.variable} h-full antialiased`}
    >
      {/* The shell owns its own layout now — a flex body would fight the fixed
          header and rail it lays out for itself. */}
      <body className="min-h-full">{children}</body>
    </html>
  );
}
