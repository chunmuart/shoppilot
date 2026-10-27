import { memo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** 不执行模型输出中的 HTML；展开区域保留原始全文与 Markdown 结构。 */
function MarkdownBody({ content }: { content: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children, href }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="markdown-table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function Markdown({
  content,
  streaming = false,
}: {
  content: string;
  streaming?: boolean;
}) {
  if (streaming || (content.length <= 320 && content.split("\n").length <= 12))
    return <MarkdownBody content={content} />;
  const firstParagraph = content.trimStart().split(/\n\s*\n/, 1)[0];
  // 极长单段只收起展示摘要，完整原文仍在原生 details 中，随时可展开。
  const preview =
    firstParagraph.length <= 400
      ? firstParagraph
      : `${firstParagraph.slice(0, 240)}…`;
  return (
    <div className="answer-collapsible">
      <div className="answer-preview">
        <MarkdownBody content={preview} />
      </div>
      <details className="answer-details">
        <summary>
          <span className="expand-label">展开完整选购建议</span>
          <span className="collapse-label">收起完整选购建议</span>
          <span className="expand-chevron" aria-hidden="true">
            ⌄
          </span>
        </summary>
        <MarkdownBody content={content} />
      </details>
    </div>
  );
}
export default memo(Markdown);
