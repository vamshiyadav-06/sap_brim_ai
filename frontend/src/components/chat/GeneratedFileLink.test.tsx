import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../services/api', () => ({ apiDownloadGeneratedFile: vi.fn() }));

import { AnswerMarkdown } from './AnswerMarkdown';
import { generatedFileId } from './GeneratedFileLink';

const ID = 'a'.repeat(32);

describe('generated document links', () => {
  it('recognises only the exact relative link the server writes', () => {
    expect(generatedFileId(`/generated-files/${ID}`)).toBe(ID);
    expect(generatedFileId(`https://evil.example/generated-files/${ID}`)).toBeNull();
    expect(generatedFileId('/generated-files/../../etc/passwd')).toBeNull();
    expect(generatedFileId(`/generated-files/${ID}/x`)).toBeNull();
    expect(generatedFileId(undefined)).toBeNull();
  });

  it('is shown as a download button with the file name, not as a plain link', () => {
    const html = renderToStaticMarkup(
      <AnswerMarkdown content={`I've created **Billing** for you.\n\n[Billing_Overview.pdf](/generated-files/${ID})\n\n**Includes:** Intro, Steps`} />
    );
    expect(html).toContain('<button');
    expect(html).toContain('Billing_Overview.pdf');
    expect(html).toContain('Click to download');
    expect(html).not.toContain('href="/generated-files');
    expect(html).toContain('Includes:');
  });

  it('keeps ordinary links safe and other relative links as plain text', () => {
    const html = renderToStaticMarkup(<AnswerMarkdown content={'[SAP](https://help.sap.com) and [x](/somewhere/else)'} />);
    expect(html).toContain('href="https://help.sap.com"');
    expect(html).toContain('rel="noopener noreferrer nofollow"');
    expect(html).not.toContain('/somewhere/else');
  });
});
