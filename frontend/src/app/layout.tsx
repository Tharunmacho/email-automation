import type { Metadata } from "next";
import "./globals.css";
import "./product.css";

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
      className="h-full antialiased"
    >
      {/* The shell owns its own layout now — a flex body would fight the fixed
          header and rail it lays out for itself. */}
      <body className="min-h-full">{children}</body>
    </html>
  );
}
