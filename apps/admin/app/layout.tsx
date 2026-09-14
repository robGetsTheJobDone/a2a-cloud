import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "A2A Admin",
  description: "A2A platform administration",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
