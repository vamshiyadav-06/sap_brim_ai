import React, { useCallback, useEffect, useState } from 'react';
import { UserPlus } from 'lucide-react';
import { apiAdminGetSecurityControls, apiAdminSetRegistration } from '../../services/api';

export const NewUserSignInToggle: React.FC = () => {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const controls = await apiAdminGetSecurityControls();
      setEnabled(controls.registration_enabled);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load sign-up status.');
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggle = async () => {
    if (enabled === null || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await apiAdminSetRegistration(!enabled);
      setEnabled(result.registration_enabled);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not update sign-up availability.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => void toggle()}
        disabled={enabled === null || busy}
        aria-label={enabled ? 'Disable new user sign-in' : 'Enable new user sign-in'}
        title={error || (enabled ? 'Turn off new user account creation' : 'Allow new users to create accounts and sign in')}
        className={`inline-flex items-center justify-center gap-2 h-9 px-3.5 rounded-lg border text-xs font-semibold transition cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60 ${
          enabled
            ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-800 dark:text-emerald-300 hover:bg-emerald-500/20'
            : 'bg-amber-500/10 border-amber-500/30 text-amber-800 dark:text-amber-300 hover:bg-amber-500/20'
        }`}
      >
        <UserPlus size={14} />
        <span>{enabled === null ? 'Loading sign-up…' : enabled ? 'Disable new user sign-in' : 'Enable new user sign-in'}</span>
        {enabled !== null && <span className="rounded-full bg-black/5 dark:bg-white/10 px-1.5 py-0.5 text-[10px] leading-none">{enabled ? 'ON' : 'OFF'}</span>}
      </button>
      {/* Overlaid so an error never changes the header height or shifts neighbouring buttons. */}
      {error && (
        <span
          role="alert"
          className="absolute right-0 top-full z-10 mt-1 w-64 rounded-lg border border-rose-500/30 bg-white dark:bg-[#0d1018] px-2.5 py-1.5 text-[11px] text-rose-700 dark:text-rose-300 shadow-md"
        >
          {error}
        </span>
      )}
    </div>
  );
};
