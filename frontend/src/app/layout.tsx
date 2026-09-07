import type { Metadata } from "next";
import { Nunito } from "next/font/google";
import "./globals.css";

// One Nunito variable family, product-wide. Both upright and italic cuts are
// loaded, and every control inherits the same root variable.
//
// The variable is `--font-display`; `globals.css` aliases the sheet's existing
// `--font-outfit` name onto it in one line, so re-keying the face here re-keys
// the whole product without touching the thousands of rules written against
// the old name.
const display = Nunito({
  variable: "--font-display",
  subsets: ["latin"],
  style: ["normal", "italic"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "ADIRA-Master CRM",
  description: "ADIRA-Master CRM for candidate sourcing, recruitment operations, and review.",
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
