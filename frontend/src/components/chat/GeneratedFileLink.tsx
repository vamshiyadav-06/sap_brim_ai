import { useState } from 'react';
import { Download, File as FileIcon, FileCode, FileSpreadsheet, FileText, Loader2, Presentation } from 'lucide-react';
import { apiDownloadGeneratedFile } from '../../services/api';

/** The relative link the server puts in a message for a document it created: /generated-files/<32 hex>. */
const GENERATED_LINK = /^\/generated-files\/([0-9a-f]{32})$/;

/** The file id when `href` is a link to a generated document, otherwise null. */
export const generatedFileId = (href?: string): string | null => GENERATED_LINK.exec(href ?? '')?.[1] ?? null;

const iconFor = (filename: string) => {
  const ext = filename.split('.').pop()?.toLowerCase();
  if (ext === 'xlsx' || ext === 'csv') return FileSpreadsheet;
  if (ext === 'pptx') return Presentation;
  if (ext === 'pdf' || ext === 'docx' || ext === 'txt') return FileText;
  if (ext === 'html' || ext === 'md') return FileCode;
  return FileIcon;
};

/** A download card for a generated document. The file is fetched with the user's login, not a public link. */
export function GeneratedFileLink({ fileId, filename }: { fileId: string; filename: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const Icon = iconFor(filename);

  const download = async () => {
    setBusy(true);
    setError(null);
    try {
      await apiDownloadGeneratedFile(fileId, filename);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The file could not be downloaded.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="inline-flex flex-col gap-1 my-1 align-middle">
      <button
        type="button"
        onClick={() => void download()}
        disabled={busy}
        title={`Download ${filename}`}
        className="inline-flex items-center gap-3 max-w-full px-3 py-2 rounded-xl border border-slate-200 dark:border-[#2a2a3a] bg-white dark:bg-[#12121a] hover:border-orange-400 dark:hover:border-orange-500/60 text-left transition cursor-pointer disabled:opacity-70"
      >
        <span className="w-9 h-9 rounded-lg bg-orange-500/10 text-orange-600 dark:text-orange-400 flex items-center justify-center border border-orange-500/25 shrink-0">
          <Icon className="w-4 h-4" />
        </span>
        <span className="flex flex-col min-w-0">
          <span className="text-sm font-semibold text-slate-900 dark:text-slate-100 truncate">{filename}</span>
          <span className="text-[11px] text-slate-500 dark:text-slate-400">{busy ? 'Preparing download…' : 'Click to download'}</span>
        </span>
        {busy ? (
          <Loader2 className="w-4 h-4 text-orange-500 animate-spin shrink-0" />
        ) : (
          <Download className="w-4 h-4 text-slate-500 dark:text-slate-400 shrink-0" />
        )}
      </button>
      {error && <span role="alert" className="text-[11px] text-rose-500">{error}</span>}
    </span>
  );
}
