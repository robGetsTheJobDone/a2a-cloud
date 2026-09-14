import type { Metadata } from "next";
import { ConsentBanner } from "@a2a/design-system/react";
import "./globals.css";

export const metadata: Metadata = {
  title: "a2a — Docs",
  description:
    "Documentation for a2a — full A2A compliant AI agents, marketplace "
    + "deployment, sandboxed execution, scoped grants, and the a2a_pack "
    + "Python SDK.",
  metadataBase: new URL("https://docs.a2acloud.io"),
  manifest: "/brand/site.webmanifest",
  icons: {
    icon: [
      { url: "/brand/favicon.svg", type: "image/svg+xml" },
      { url: "/brand/icon-32.png", sizes: "32x32", type: "image/png" },
    ],
    apple: [
      { url: "/brand/apple-touch-icon.png", sizes: "180x180", type: "image/png" },
    ],
  },
  openGraph: {
    title: "a2a Docs",
    description:
      "Documentation for full A2A compliant agents on a2a.",
    url: "https://docs.a2acloud.io",
    siteName: "a2a docs",
    type: "website",
    images: [
      {
        url: "/brand/og-image.png",
        width: 1200,
        height: 630,
        alt: "a2a cloud documentation",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "a2a Docs",
    description: "Full A2A compliant agents, made easy with a2a-pack.",
    images: ["/brand/og-image.png"],
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-neutral-950 text-neutral-50 antialiased">
        {children}
        <ConsentBanner />
      </body>
    </html>
  );
}
