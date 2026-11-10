import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { markHtmlBreaks, remarkHtmlBreaks } from './htmlBreaks';
import { repairCollapsedMarkdown } from './repairMarkdown';
import { GeneratedFileLink, generatedFileId } from './GeneratedFileLink';

/**
 * Renders an answer's Markdown the way the chat does: images suppressed (F-10), only http(s)/mailto
 * links kept and opened safely, tables and code blocks scroll horizontally. Shared by the chat and
 * the organisation knowledge hub so saved answers look exactly like they did in the conversation.
 */
export function AnswerMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkHtmlBreaks]}
      components={{
        img: () => null,
        a: ({ href, children }) => {
          const fileId = generatedFileId(href);
          if (fileId) {
            const filename = (Array.isArray(children) ? children.join('') : String(children ?? '')).trim() || 'document';
            return <GeneratedFileLink fileId={fileId} filename={filename} />;
          }
          const safe = href && /^(https?:|mailto:)/i.test(href);
          return safe ? <a href={href} target="_blank" rel="noopener noreferrer nofollow">{children}</a> : <>{children}</>;
        },
        table: ({ node: _node, ...props }) => (
          <div className="overflow-x-auto my-3 border border-slate-200 dark:border-[#1e1e2c] rounded-xl bg-white dark:bg-[#08080c]">
            <table className="min-w-full divide-y divide-slate-200 dark:divide-[#1e1e2c] text-left text-xs" {...props} />
          </div>
        ),
        th: ({ node: _node, ...props }) => (
          <th className="px-3 py-2 bg-slate-100 dark:bg-[#0e0e16] font-semibold text-slate-800 dark:text-slate-100 border-b border-slate-200 dark:border-[#1e1e2c]" {...props} />
        ),
        td: ({ node: _node, ...props }) => (
          <td className="px-3 py-2 border-t border-slate-100 dark:border-[#161622] text-slate-700 dark:text-slate-300" {...props} />
        ),
        pre: ({ node: _node, ...props }) => (
          <div className="overflow-x-auto rounded-xl bg-slate-50 dark:bg-[#06060a] p-3.5 my-2 border border-slate-200 dark:border-[#1c1c28]">
            <pre {...props} />
          </div>
        ),
        code: ({ node: _node, className, children, ...props }) => {
          const isInline = !className;
          return isInline ? (
            <code className="px-1.5 py-0.5 rounded bg-orange-50 dark:bg-[#0f0f18] text-orange-600 dark:text-orange-400 font-mono text-[12px] border border-orange-200 dark:border-orange-500/25" {...props}>
              {children}
            </code>
          ) : (
            <code className={className} {...props}>
              {children}
            </code>
          );
        },
      }}
    >
      {markHtmlBreaks(repairCollapsedMarkdown(content))}
    </ReactMarkdown>
  );
}
