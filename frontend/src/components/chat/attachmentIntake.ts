// Which files of a pick/drop (single files, a whole folder, dropped folders) are sent to the server to be read,
// and which links in a message are read too. Pure helpers: no React, so they are unit-tested.

/** Files, folder files and linked pages together per message (the server enforces the same cap). */
export const MAX_ATTACHMENTS = 20;
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const MAX_LINKS_PER_MESSAGE = 3;

/** A file with the path it has inside the picked or dropped folder (just its name for a single file). */
export interface PickedFile {
  file: File;
  path: string;
}

const SKIPPED_FOLDERS = new Set([
  'node_modules', '__pycache__', 'dist', 'build', 'target', 'venv', 'env', 'out', 'coverage', 'bin', 'obj',
]);

// Never readable as text, so they would only use up the per-message file slots of a folder.
const UNREADABLE_EXTENSIONS = new Set([
  'exe', 'dll', 'so', 'dylib', 'bin', 'class', 'jar', 'o', 'obj', 'pyc', 'pyd', 'lock', 'map', 'iso', 'dmg', 'msi',
  'woff', 'woff2', 'ttf', 'otf', 'eot', 'mp3', 'wav', 'mp4', 'mov', 'avi', 'mkv', 'webm', 'sqlite', 'db', 'ico',
]);

export const fileExtension = (name: string): string => {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
};

/** "docs/specs/a.pdf" -> "docs › specs › a.pdf" (the server keeps only what follows the last "/" of a name). */
export const displayName = (path: string): string => path.split('/').filter(Boolean).join(' › ');

export interface Selection {
  accepted: PickedFile[];
  skipped: number;
}

/**
 * The files to read: single files only have to be a sensible size; files that come from a folder also skip
 * hidden files and folders and build/dependency folders. At most `room` are accepted; the rest count as skipped.
 */
export const selectReadableFiles = (picked: PickedFile[], room: number): Selection => {
  const usable = picked.filter(({ file, path }) => {
    if (file.size === 0 || file.size > MAX_FILE_BYTES) return false;
    if (UNREADABLE_EXTENSIONS.has(fileExtension(file.name))) return false;
    const parts = path.split('/').filter(Boolean);
    if (parts.length <= 1) return true; // chosen on its own: the user meant it
    return !parts.some((part, i) => part.startsWith('.') || (i < parts.length - 1 && SKIPPED_FOLDERS.has(part.toLowerCase())));
  });
  const accepted = usable.slice(0, Math.max(0, room));
  return { accepted, skipped: picked.length - accepted.length };
};

export const isHttpUrl = (value: string): boolean => /^https?:\/\/[^\s]+$/i.test(value.trim());

/** http(s) links in a message, in order, without trailing punctuation and duplicates. */
export const extractUrls = (text: string, limit = MAX_LINKS_PER_MESSAGE): string[] => {
  const found: string[] = [];
  for (const match of text.match(/https?:\/\/[^\s<>"'`]+/gi) ?? []) {
    const url = match.replace(/[.,;:!?)\]}>'"]+$/, '');
    if (url && !found.includes(url)) found.push(url);
    if (found.length >= limit) break;
  }
  return found;
};

/** A short label for a link chip: host plus path, without the scheme. */
export const linkLabel = (url: string): string => {
  try {
    const parsed = new URL(url);
    const label = `${parsed.hostname}${parsed.pathname === '/' ? '' : parsed.pathname}`;
    return label.length > 60 ? `${label.slice(0, 57)}...` : label;
  } catch {
    return url.slice(0, 60);
  }
};

// --- Dropped folders (webkitGetAsEntry) -------------------------------------------------------------------

interface FsEntry {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  fullPath: string;
  file?: (ok: (file: File) => void, fail: (error: unknown) => void) => void;
  createReader?: () => { readEntries: (ok: (entries: FsEntry[]) => void, fail: (error: unknown) => void) => void };
}

const readAllEntries = async (directory: FsEntry): Promise<FsEntry[]> => {
  const reader = directory.createReader?.();
  if (!reader) return [];
  const all: FsEntry[] = [];
  // readEntries returns the entries in batches; an empty batch ends the directory.
  for (;;) {
    const batch = await new Promise<FsEntry[]>((resolve) => reader.readEntries(resolve, () => resolve([])));
    if (batch.length === 0) return all;
    all.push(...batch);
  }
};

const walk = async (entry: FsEntry, out: PickedFile[], limit: number): Promise<void> => {
  if (out.length >= limit) return;
  if (entry.isFile && entry.file) {
    const file = await new Promise<File | null>((resolve) => entry.file!(resolve, () => resolve(null)));
    if (file) out.push({ file, path: entry.fullPath.replace(/^\//, '') });
  } else if (entry.isDirectory) {
    for (const child of await readAllEntries(entry)) await walk(child, out, limit);
  }
};

/** Every file of a drop, including the files inside dropped folders (up to `limit` are collected). */
export const collectDropped = async (data: DataTransfer, limit = 500): Promise<PickedFile[]> => {
  const entries = Array.from(data.items ?? [])
    .map((item) => (item as unknown as { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry?.() ?? null)
    .filter((entry): entry is FsEntry => entry !== null);
  if (entries.length === 0) return Array.from(data.files ?? []).map((file) => ({ file, path: file.name }));
  const out: PickedFile[] = [];
  for (const entry of entries) await walk(entry, out, limit);
  return out;
};

/** Run `task` over `items` with at most `size` running at once; results keep the order of `items`. */
export const inPool = async <T, R>(items: T[], size: number, task: (item: T) => Promise<R>): Promise<R[]> => {
  const results: R[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      results[index] = await task(items[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(size, items.length) }, worker));
  return results;
};
