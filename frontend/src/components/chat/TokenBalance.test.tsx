import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { TokenBalance, refillText } from './TokenBalance';

const NOW = Date.parse('2026-11-05T12:00:00Z');

describe('TokenBalance', () => {
  it('shows what is left of the 100 and when it refills', () => {
    const html = renderToStaticMarkup(
      <TokenBalance wallet={{ balance: 98.75, refillAt: '2026-11-07T12:00:00' }} now={NOW} />,
    );
    expect(html).toContain('98.75 of 100 tokens left');
    expect(html).toContain('refills in 2 days');
  });
  it('says the tokens are used up at zero, and still says when they come back', () => {
    const html = renderToStaticMarkup(<TokenBalance wallet={{ balance: 0, refillAt: '2026-11-05T20:00:00' }} now={NOW} />);
    expect(html).toContain('You have used all your tokens.');
    expect(html).toContain('refills in 8 hours');
  });
  it('shows nothing for accounts that are not metered', () => {
    expect(renderToStaticMarkup(<TokenBalance wallet={null} />)).toBe('');
  });
  it('words the refill time', () => {
    expect(refillText('2026-11-05T12:30:00', NOW)).toBe('soon');
    expect(refillText('2026-11-06T12:00:00', NOW)).toBe('in 1 day');
    expect(refillText(null, NOW)).toBeNull();
  });
});
