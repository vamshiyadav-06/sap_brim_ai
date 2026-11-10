import { describe, expect, it } from 'vitest';
import {
  MAX_FILE_BYTES,
  displayName,
  extractUrls,
  inPool,
  isHttpUrl,
  linkLabel,
  selectReadableFiles,
  type PickedFile,
} from './attachmentIntake';

const pick = (path: string, size = 100): PickedFile => ({
  file: new File(['x'.repeat(Math.min(size, 50))], path.split('/').pop() ?? path, { type: 'text/plain' }),
  path,
});
// File.size follows the content; for size checks build the object explicitly.
const sized = (path: string, size: number): PickedFile => {
  const picked = pick(path);
  Object.defineProperty(picked.file, 'size', { value: size });
  return picked;
};

describe('selectReadableFiles', () => {
  it('keeps single files the user chose, even hidden-looking ones', () => {
    const { accepted, skipped } = selectReadableFiles([pick('.env.example'), pick('notes.txt')], 20);
    expect(accepted.map((p) => p.path)).toEqual(['.env.example', 'notes.txt']);
    expect(skipped).toBe(0);
  });

  it('skips hidden files, build folders, binaries, empty and oversized files from a folder', () => {
    const { accepted, skipped } = selectReadableFiles([
      pick('proj/readme.md'),
      pick('proj/.git/config'),
      pick('proj/.hidden.txt'),
      pick('proj/node_modules/x/index.js'),
      pick('proj/tool.exe'),
      sized('proj/empty.txt', 0),
      sized('proj/huge.pdf', MAX_FILE_BYTES + 1),
      pick('proj/src/app.py'),
    ], 20);
    expect(accepted.map((p) => p.path)).toEqual(['proj/readme.md', 'proj/src/app.py']);
    expect(skipped).toBe(6);
  });

  it('accepts at most the free slots and counts the rest as skipped', () => {
    const files = Array.from({ length: 30 }, (_, i) => pick(`f/${i}.txt`));
    const { accepted, skipped } = selectReadableFiles(files, 20);
    expect(accepted).toHaveLength(20);
    expect(skipped).toBe(10);
    expect(selectReadableFiles(files, 0).accepted).toHaveLength(0);
  });
});

describe('names and links', () => {
  it('shows a folder path without "/" so the server keeps the whole name', () => {
    expect(displayName('docs/specs/a.pdf')).toBe('docs › specs › a.pdf');
    expect(displayName('a.pdf')).toBe('a.pdf');
  });

  it('finds links in a message without trailing punctuation or duplicates, at most three', () => {
    expect(extractUrls('See https://a.com/x?y=1. and (http://b.org/p), https://a.com/x?y=1 again')).toEqual([
      'https://a.com/x?y=1',
      'http://b.org/p',
    ]);
    expect(extractUrls('one https://1.io two https://2.io three https://3.io four https://4.io')).toHaveLength(3);
    expect(extractUrls('no links')).toEqual([]);
  });

  it('recognises only http(s) addresses and labels them briefly', () => {
    expect(isHttpUrl('https://example.com/a')).toBe(true);
    expect(isHttpUrl('ftp://example.com')).toBe(false);
    expect(isHttpUrl('example.com')).toBe(false);
    expect(linkLabel('https://help.sap.com/docs/brim/overview')).toBe('help.sap.com/docs/brim/overview');
    expect(linkLabel('https://example.com/')).toBe('example.com');
  });
});

describe('inPool', () => {
  it('runs with limited concurrency and keeps the order of results', async () => {
    let running = 0;
    let peak = 0;
    const results = await inPool([1, 2, 3, 4, 5, 6], 2, async (n) => {
      running++;
      peak = Math.max(peak, running);
      await new Promise((r) => setTimeout(r, 5));
      running--;
      return n * 10;
    });
    expect(results).toEqual([10, 20, 30, 40, 50, 60]);
    expect(peak).toBeLessThanOrEqual(2);
  });
});
