import { marked } from "marked";
import hljs from "highlight.js/lib/core";
import python from "highlight.js/lib/languages/python";
import bash from "highlight.js/lib/languages/bash";
import json from "highlight.js/lib/languages/json";
import typescript from "highlight.js/lib/languages/typescript";
import javascript from "highlight.js/lib/languages/javascript";
import go from "highlight.js/lib/languages/go";
import rust from "highlight.js/lib/languages/rust";

hljs.registerLanguage("python", python);
hljs.registerLanguage("py", python);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("sh", bash);
hljs.registerLanguage("json", json);
hljs.registerLanguage("ts", typescript);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("js", javascript);
hljs.registerLanguage("go", go);
hljs.registerLanguage("rust", rust);

const renderer = new marked.Renderer();
renderer.code = ({ text, lang }) => {
  const language = lang && hljs.getLanguage(lang) ? lang : null;
  const html = language
    ? hljs.highlight(text, { language }).value
    : escapeHtml(text);
  const cls = language ? `hljs language-${language}` : "hljs";
  return `<pre><code class="${cls}">${html}</code></pre>`;
};
renderer.heading = function ({ tokens, depth }) {
  const html = this.parser.parseInline(tokens);
  const plain = tokens
    .map((token) => ("text" in token ? String(token.text) : token.raw))
    .join(" ");
  const id = plain
    .toLowerCase()
    .replace(/<[^>]+>/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `<h${depth} id="${escapeHtml(id)}">${html}</h${depth}>`;
};

marked.setOptions({ renderer, gfm: true });

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function Markdown({ source }: { source: string }) {
  const html = marked.parse(source, { async: false }) as string;
  return (
    <article
      className="prose max-w-none"
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
