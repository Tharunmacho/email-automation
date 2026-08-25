import type { Metadata } from "next";
import { Plus_Jakarta_Sans } from "next/font/google";
import "./globals.css";

// One family, product-wide. Plus Jakarta Sans is a variable font on Google
// Fonts, so the whole 200–800 axis arrives in a single file and every weight
// the sheet asks for is a real cut rather than a synthetic smear. Its digits
// are wide and near-circular, which is what a dashboard built out of large
// numbers needs — a KPI set in a narrow grotesque reads as text, not as a
// figure. Nothing else is loaded: the places that used to reach for Inter,
// Poppins or a monospace take this same face, with `tabular-nums` and
// letter-spacing doing the work a second family used to.
//
// The variable is `--font-display`; `globals.css` aliases the sheet's existing
// `--font-outfit` name onto it in one line, so re-keying the face here re-keys
// the whole product without touching the thousands of rules written against
// the old name.
const display = Plus_Jakarta_Sans({
  variable: "--font-display",
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
      className={`${display.variable} h-full antialiased`}
    >
      {/* The shell owns its own layout now — a flex body would fight the fixed
          header and rail it lays out for itself. */}
      <body className="min-h-full">{children}</body>
    </html>
  );
}
