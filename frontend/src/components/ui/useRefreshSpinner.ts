import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Visible feedback for a Refresh button. `start()` on click turns `spinning` on; `stop()` when the reload
 * has finished turns it off, but never sooner than `minMs` after the click, so a fast reload still shows the
 * icon turning. `stop()` before any `start()` (the first automatic load) does nothing.
 */
export function useRefreshSpinner(minMs = 700) {
  const [spinning, setSpinning] = useState(false);
  const startedAt = useRef(0);
  const timer = useRef<number | undefined>(undefined);

  const start = useCallback(() => {
    window.clearTimeout(timer.current);
    startedAt.current = Date.now();
    setSpinning(true);
  }, []);

  const stop = useCallback(() => {
    if (!startedAt.current) return;
    const wait = Math.max(0, minMs - (Date.now() - startedAt.current));
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      startedAt.current = 0;
      setSpinning(false);
    }, wait);
  }, [minMs]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  return { spinning, start, stop } as const;
}
