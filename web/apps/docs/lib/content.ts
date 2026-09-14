/* Filesystem-backed content loader.
 *
 * Pages live under ../content as plain Markdown. The slug for a page is
 * its path relative to content/ minus ".md", with index.md → "".
 */
import fs from "node:fs";
import path from "node:path";

const ROOT = path.join(process.cwd(), "content");

export type PageRef = {
  slug: string;     // "" | "quickstart" | "concepts/agents" | "reference/agent"
  title: string;
  group: string;    // top-level dir, e.g. "concepts" | "reference"
  filepath: string; // absolute
};

export type Page = PageRef & { markdown: string };

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, out);
    else if (entry.isFile() && entry.name.endsWith(".md")) out.push(full);
  }
  return out;
}

function slugFromPath(filepath: string): string {
  const rel = path.relative(ROOT, filepath).replace(/\\/g, "/");
  let slug = rel.replace(/\.md$/, "");
  if (slug === "index") slug = "";
  if (slug.endsWith("/index")) slug = slug.slice(0, -"/index".length);
  return slug;
}

function titleFromMarkdown(md: string, fallback: string): string {
  const m = md.match(/^#\s+(.+?)\s*$/m);
  return m ? m[1].replace(/`/g, "") : fallback;
}

export function allPages(): PageRef[] {
  const files = walk(ROOT);
  return files.map((filepath) => {
    const slug = slugFromPath(filepath);
    const md = fs.readFileSync(filepath, "utf-8");
    const fallback = slug.split("/").pop() || "home";
    return {
      slug,
      title: titleFromMarkdown(md, fallback),
      group: slug.split("/")[0] || "home",
      filepath,
    };
  });
}

export function getPage(slug: string): Page | null {
  const target = slug === "" ? "" : slug;
  for (const ref of allPages()) {
    if (ref.slug === target) {
      return { ...ref, markdown: fs.readFileSync(ref.filepath, "utf-8") };
    }
  }
  return null;
}

export function navTree(): Record<string, PageRef[]> {
  const tree: Record<string, PageRef[]> = {};
  for (const p of allPages()) {
    const key = p.group || "home";
    (tree[key] ||= []).push(p);
  }
  for (const k of Object.keys(tree)) tree[k].sort((a, b) => a.slug.localeCompare(b.slug));
  return tree;
}

export function allMarkdownConcatenated(): string {
  const pages = allPages().sort((a, b) => a.slug.localeCompare(b.slug));
  return pages
    .map((p) => `\n\n---\n\n# Page: ${p.slug || "(home)"}\n\n${fs.readFileSync(p.filepath, "utf-8")}`)
    .join("\n");
}
