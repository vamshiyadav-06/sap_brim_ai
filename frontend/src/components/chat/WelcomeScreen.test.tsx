import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { WelcomeScreen } from './WelcomeScreen';
import { MODES, SUGGESTIONS } from './welcomeContent';
import { DEFAULT_CHAT_MODE, type ChatMode } from '../../types';
import { nextChatMode } from './ModeToggle';

const render = (mode: ChatMode) =>
  renderToStaticMarkup(<WelcomeScreen mode={mode} onModeChange={() => {}} onSelectPrompt={() => {}} />);

describe('WelcomeScreen', () => {
  it('has a High button that is off by default in the RAG Pipeline mode', () => {
    const rag = render('scholar');
    const direct = render('sprint');

    expect(rag.match(/<button[^>]*aria-label="High mode"/g)).toHaveLength(1);
    expect(rag).toContain('aria-pressed="false"');
    expect(direct).toContain('aria-pressed="true"');
    expect(rag).toContain('RAG Pipeline is the default');
    expect(direct).toContain('High is on');
  });

  it('defaults to the RAG Pipeline and toggles between the existing routes', () => {
    expect(DEFAULT_CHAT_MODE).toBe('scholar');
    expect(nextChatMode(DEFAULT_CHAT_MODE)).toBe('sprint');
    expect(nextChatMode('sprint')).toBe('scholar');
  });

  it.each<ChatMode>(['sprint', 'scholar'])('shows the %s suggestions only', (mode) => {
    const other: ChatMode = mode === 'sprint' ? 'scholar' : 'sprint';
    const html = render(mode);

    for (const { title } of SUGGESTIONS[mode]) expect(html).toContain(title);
    for (const { title } of SUGGESTIONS[other]) {
      if (!SUGGESTIONS[mode].some((s) => s.title === title)) expect(html).not.toContain(title);
    }
  });

  it('sets SAP-only expectations and explains both modes', () => {
    const html = render('scholar');

    expect(html).not.toContain('Ask any question');
    expect(html).toContain('SAP BRIM');
    expect(html).toContain(MODES.find((mode) => mode.id === 'scholar')!.detail);
    expect(render('sprint')).toContain(MODES.find((mode) => mode.id === 'sprint')!.detail);
    expect(html).toContain('AI answers can contain mistakes');
  });

  it('offers distinct, SAP-specific prompts', () => {
    const prompts = [...SUGGESTIONS.sprint, ...SUGGESTIONS.scholar].map((s) => s.prompt);

    expect(new Set(prompts).size).toBe(prompts.length);
    for (const prompt of prompts) expect(prompt).toMatch(/SAP|BRIM|FI-CA|Convergent/);
  });
});
