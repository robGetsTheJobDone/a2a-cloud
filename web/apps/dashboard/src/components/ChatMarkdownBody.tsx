import { isValidElement, memo, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "highlight.js/styles/github-dark.css";
import { CopyButton } from "./DashboardChrome";

const MarkdownBody = memo(function MarkdownBody({ content }: { content: string }) {
  return (
    <div className="chat-md break-anywhere">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ node: _node, ...props }) => (
            <a
              {...props}
              target="_blank"
              rel="noreferrer noopener"
              className="text-signal-protocol underline decoration-signal-protocol/50 underline-offset-2 hover:text-signal-protocol hover:decoration-signal-protocol"
            />
          ),
          p: ({ node: _node, ...props }) => (
            <p {...props} className="my-2 leading-relaxed first:mt-0 last:mb-0" />
          ),
          h1: ({ node: _node, ...props }) => (
            <h1 {...props} className="mb-2 mt-4 text-lg font-semibold text-ink first:mt-0" />
          ),
          h2: ({ node: _node, ...props }) => (
            <h2 {...props} className="mb-2 mt-4 text-base font-semibold text-ink first:mt-0" />
          ),
          h3: ({ node: _node, ...props }) => (
            <h3 {...props} className="mb-1.5 mt-3 text-sm font-semibold text-ink first:mt-0" />
          ),
          ul: ({ node: _node, ...props }) => (
            <ul {...props} className="my-2 list-disc space-y-1 pl-5 marker:text-ink-faint" />
          ),
          ol: ({ node: _node, ...props }) => (
            <ol {...props} className="my-2 list-decimal space-y-1 pl-5 marker:text-ink-muted" />
          ),
          li: ({ node: _node, ...props }) => <li {...props} className="leading-relaxed" />,
          blockquote: ({ node: _node, ...props }) => (
            <blockquote
              {...props}
              className="my-3 border-l-2 border-runtime-line/70 bg-runtime-panel/40 px-3 py-1 italic text-ink-soft"
            />
          ),
          hr: ({ node: _node, ...props }) => (
            <hr {...props} className="my-4 border-runtime-line-soft/60" />
          ),
          strong: ({ node: _node, ...props }) => (
            <strong {...props} className="font-semibold text-ink" />
          ),
          em: ({ node: _node, ...props }) => <em {...props} className="italic text-ink-soft" />,
          table: ({ node: _node, ...props }) => (
            <div className="my-3 overflow-x-auto rounded-md border border-runtime-line-soft/60">
              <table {...props} className="min-w-full text-left text-xs" />
            </div>
          ),
          thead: ({ node: _node, ...props }) => (
            <thead {...props} className="bg-runtime-panel/70 text-ink-soft" />
          ),
          th: ({ node: _node, ...props }) => (
            <th {...props} className="border-b border-runtime-line-soft/60 px-3 py-1.5 font-medium" />
          ),
          td: ({ node: _node, ...props }) => (
            <td {...props} className="border-b border-runtime-line-soft/60 px-3 py-1.5 align-top" />
          ),
          code: ({ node: _node, className: cn, children, ...props }) => {
            const isBlock = /language-/.test(cn || "");
            if (isBlock) {
              return (
                <code {...props} className={`${cn || ""} hljs`}>
                  {children}
                </code>
              );
            }
            return (
              <code
                {...props}
                className="break-anywhere rounded-md bg-runtime-raised/70 px-1 py-[1px] font-mono text-[0.85em] text-ink"
              >
                {children}
              </code>
            );
          },
          pre: ({ node: _node, children, ...props }) => (
            <CodeBlock>
              <pre
                {...props}
                className="overflow-x-auto rounded-lg border border-runtime-line-soft/60 bg-[#0d1117] px-3 py-2.5 font-mono text-[12.5px] leading-relaxed"
              >
                {children}
              </pre>
            </CodeBlock>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});

function CodeBlock({ children }: { children: ReactNode }) {
  const text = reactNodeText(children);
  return (
    <div className="group relative my-3">
      <CopyButton
        value={text}
        label="Copy code"
        copiedLabel="copied"
        className="absolute right-2 top-2 z-10 opacity-0 transition group-hover:opacity-100"
      >
        copy
      </CopyButton>
      {children}
    </div>
  );
}

function reactNodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(reactNodeText).join("");
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return reactNodeText(node.props.children);
  }
  return "";
}

export default MarkdownBody;
