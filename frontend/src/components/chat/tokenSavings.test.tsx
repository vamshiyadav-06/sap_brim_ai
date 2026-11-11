import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { TokenMeterBadge } from './TokenMeterBadge';
import { TokenSavedToast } from './TokenSavedToast';
import { formatTokens, savingsMessage } from './tokenSavings';

const SAVED = { used: 900, baseline: 4000, saved: 3100 };
const NOTHING_SAVED = { used: 700, baseline: 500, saved: 0 };

describe('savingsMessage', () => {
  it('tells the user how many tokens they saved compared with other AI chatbots, not how many were used', () => {
    const message = savingsMessage(SAVED);

    expect(message.saved).toBe(true);
    expect(message.emoji).toBe('😊');
    expect(message.title).toBe('You saved ~3,100 tokens! 🎉');
    expect(message.detail).toBe("That's 78% fewer tokens than other AI chatbots would use for this question, counted 1 token for 1 token. 😊");
    expect(`${message.title} ${message.detail}`).not.toContain('900');
  });

  it('shows small savings as they are, never rounded down to 0%', () => {
    const message = savingsMessage({ used: 9980, baseline: 10000, saved: 20 });

    expect(message.title).toBe('You saved ~20 tokens! 🎉');
    expect(message.detail).toContain('1% fewer');
    expect(savingsMessage({ used: 9, baseline: 10, saved: 1 }).title).toBe('You saved ~1 token! 🎉');
  });

  it('always says tokens were saved, never "none" or "lean"', () => {
    for (const usage of [NOTHING_SAVED, { ...NOTHING_SAVED, cost: 0.4 }, { used: 0, baseline: 0, saved: 0 }]) {
      const message = savingsMessage(usage);
      expect(message.saved).toBe(true);
      expect(message.title).toMatch(/You saved/);
      expect(`${message.title} ${message.detail}`).not.toMatch(/lean|no tokens|not saved|none/i);
      expect(message.title).not.toMatch(/~0/);
      expect(message.title).toContain('🎉');
      expect(message.detail).toContain('😊');
    }
  });

  it('when the estimate is not positive, compares the Clyptus tokens charged with a typical chatbot', () => {
    const message = savingsMessage({ used: 700, baseline: 500, saved: 0, cost: 0.4 });

    expect(message.title).toBe('You saved ~500 tokens! 🎉');
  });

  it('formats whole, non-negative token counts', () => {
    expect(formatTokens(12345.6)).toBe('12,346');
    expect(formatTokens(-3)).toBe('0');
  });
});

describe('TokenMeterBadge', () => {
  it('shows a counting indicator without numbers while the answer streams', () => {
    const html = renderToStaticMarkup(<TokenMeterBadge streaming />);

    expect(html).toContain('😊');
    expect(html).toContain('Counting tokens…');
    expect(html).not.toContain('Saved');
  });

  it('shows the free tokens this answer used, with a smile', () => {
    const html = renderToStaticMarkup(<TokenMeterBadge usage={{ ...SAVED, cost: 0.5 }} />);

    expect(html).toContain('aria-label="You used only 0.5 tokens"');
    expect(html).toContain('😊');
    expect(html).not.toContain('3,100');
  });

  it('says "1 token" for a full-cost answer and nothing without a cost', () => {
    expect(renderToStaticMarkup(<TokenMeterBadge usage={{ ...SAVED, cost: 1 }} />)).toContain('You used only 1 token<');
    expect(renderToStaticMarkup(<TokenMeterBadge usage={SAVED} />)).toBe('');
    expect(renderToStaticMarkup(<TokenMeterBadge />)).toBe('');
  });
});

describe('TokenSavedToast', () => {
  it('pops up with the savings of the completed answer', () => {
    const html = renderToStaticMarkup(<TokenSavedToast usage={SAVED} onClose={() => {}} />);

    expect(html).toContain('role="status"');
    expect(html).toContain('You saved ~3,100 tokens!');
  });

  it('pops up after every answer and always says tokens were saved', () => {
    const html = renderToStaticMarkup(<TokenSavedToast usage={NOTHING_SAVED} onClose={() => {}} />);

    expect(html).toContain('You saved tokens on this answer!');
    expect(html).toContain('😊');
    expect(html).not.toMatch(/lean|no tokens/i);
  });

  it('does not pop up before an answer completes', () => {
    expect(renderToStaticMarkup(<TokenSavedToast usage={null} onClose={() => {}} />)).toBe('');
  });
});

describe('TokenMeterBadge with the wallet', () => {
  it('puts what is left of the 100 beside the tokens used', () => {
    const html = renderToStaticMarkup(
      <TokenMeterBadge usage={{ ...SAVED, cost: 0.5 }} wallet={{ balance: 99.5, refillAt: null }} />,
    );
    expect(html).toContain('You used only 0.5 tokens · 99.5 of 100 left');
  });
});
