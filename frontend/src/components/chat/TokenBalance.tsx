import React from 'react';

export const OUT_OF_TOKENS_MESSAGE = 'You have used all your tokens.';
/** The allowance every account is topped back up to (backend TOKEN_ALLOWANCE). */
export const TOKEN_ALLOWANCE = 100;

export interface TokenWallet {
  balance: number;
  /** ISO time of the next refill, when the server has set one. */
  refillAt?: string | null;
}

/** "in 2 days", "in 5 hours", "soon": how long until the wallet is topped up again. */
export const refillText = (refillAt: string | null | undefined, now: number = Date.now()): string | null => {
  if (!refillAt) return null;
  // The server stores UTC without a zone suffix.
  const time = Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(refillAt) ? refillAt : `${refillAt}Z`);
  if (Number.isNaN(time)) return null;
  const hours = Math.ceil((time - now) / 3_600_000);
  if (hours <= 1) return 'soon';
  if (hours < 24) return `in ${hours} hours`;
  const days = Math.ceil(hours / 24);
  return `in ${days} ${days === 1 ? 'day' : 'days'}`;
};

const fmt = (n: number) => n.toLocaleString('en-US', { maximumFractionDigits: 3 });

/** The token wallet (individual accounts), shown above the message box: what is left of the allowance and
 *  when it refills. Null means the account is not metered. */
export const TokenBalance: React.FC<{ wallet: TokenWallet | null; now?: number }> = ({ wallet, now }) => {
  if (wallet === null) return null;
  const empty = wallet.balance <= 0;
  const refill = refillText(wallet.refillAt, now);
  return (
    <p
      role="status"
      className={`mx-auto mb-2 w-fit px-3 py-1 rounded-full text-[11px] font-medium tabular-nums select-none border ${
        empty
          ? 'bg-rose-50 dark:bg-rose-950/40 text-rose-800 dark:text-rose-300 border-rose-200 dark:border-rose-800/50'
          : 'bg-orange-50 dark:bg-orange-950/30 text-orange-800 dark:text-orange-300 border-orange-200 dark:border-orange-800/50'
      }`}
    >
      {empty ? OUT_OF_TOKENS_MESSAGE : `${fmt(wallet.balance)} of ${fmt(TOKEN_ALLOWANCE)} tokens left`}
      {refill ? ` · refills ${refill}` : ''}
    </p>
  );
};

/** The latest wallet the server reported per user, so it survives leaving and re-opening the chat page. */
const lastKnown = new Map<string, TokenWallet>();
export const rememberTokenWallet = (userId: string, wallet: TokenWallet): void => void lastKnown.set(userId, wallet);
export const recallTokenWallet = (
  user: { id: string; token_balance?: number; token_refill_at?: string | null } | null,
): TokenWallet | null => {
  if (!user) return null;
  const known = lastKnown.get(user.id);
  if (known) return known;
  return typeof user.token_balance === 'number' ? { balance: user.token_balance, refillAt: user.token_refill_at } : null;
};
