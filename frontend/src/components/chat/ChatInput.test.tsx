import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ChatInput } from './ChatInput';

const render = (isLoading: boolean) =>
  renderToStaticMarkup(
    <ChatInput onSendMessage={vi.fn()} onStop={vi.fn()} isLoading={isLoading} mode="scholar" onModeChange={vi.fn()} />
  );

const tag = (html: string, name: string) => html.match(new RegExp(`<${name}[^>]*>`))?.[0] ?? '';
// The HTML attribute, not Tailwind's `disabled:` variant classes.
const isDisabled = (element: string) => / disabled(=|>|\s)/.test(element);

describe('ChatInput while an answer is being generated', () => {
  it('keeps the text box and the + button usable so the next question can be typed', () => {
    const html = render(true);
    const textarea = tag(html, 'textarea');
    const plus = html.match(/<button[^>]*title="Add files, folders or links"[^>]*>/)?.[0] ?? '';
    expect(textarea).not.toBe('');
    expect(plus).not.toBe('');
    expect(isDisabled(textarea)).toBe(false);
    expect(isDisabled(plus)).toBe(false);
  });

  it('still offers Stop instead of Send, so a second question cannot be submitted mid-answer', () => {
    const html = render(true);
    expect(html).toContain('title="Stop generating"');
    expect(html).not.toContain('title="Send message"');
  });

  it('shows Send when idle', () => {
    expect(render(false)).toContain('title="Send message"');
  });
});
