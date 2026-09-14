import fs from "node:fs";
import type { MetadataRoute } from "next";

import { allPages } from "@/lib/content";

const SITE_URL = "https://docs.a2acloud.io";

export default function sitemap(): MetadataRoute.Sitemap {
  return allPages()
    .sort((a, b) => a.slug.localeCompare(b.slug))
    .map((page) => {
      const path = page.slug ? `/${page.slug}` : "";

      return {
        url: `${SITE_URL}${path}`,
        lastModified: fs.statSync(page.filepath).mtime,
        changeFrequency: "weekly",
        priority: page.slug === "" ? 1 : 0.8,
      };
    });
}
