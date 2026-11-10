import React, { useCallback, useEffect, useState } from 'react';
import { KeyRound, RefreshCw, ShieldCheck } from 'lucide-react';
import {
  apiAdminGetSecurityControls,
  apiAdminIssueLoginUnlockKey,
  apiAdminListLoginLockouts,
} from '../../services/api';
import type { AdminSecurityControls, LoginLockout } from '../../services/api';

export const AuthenticationControls: React.FC = () => {
  const [controls, setControls] = useState<AdminSecurityControls | null>(null);
  const [lockouts, setLockouts] = useState<LoginLockout[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ email: string; key: string; expires_at: string } | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [nextControls, nextLockouts] = await Promise.all([
        apiAdminGetSecurityControls(), apiAdminListLoginLockouts(),
      ]);
      setControls(nextControls);
      setLockouts(nextLockouts.users);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load authentication controls.');
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const issueKey = async (account: LoginLockout) => {
    setBusy(account.id);
    setError(null);
    setIssued(null);
    try {
      const result = await apiAdminIssueLoginUnlockKey(account.id);
      setIssued({ email: result.email, key: result.key, expires_at: result.expires_at });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not issue an unlock key.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="p-5 md:p-6 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800 space-y-5" aria-labelledby="auth-controls-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="auth-controls-heading" className="flex items-center gap-2 text-lg font-bold text-slate-900 dark:text-white">
            <ShieldCheck size={19} className="text-amber-600 dark:text-amber-400" /> Authentication controls
          </h2>
          <p className="mt-1 text-sm text-slate-600 dark:text-zinc-400">Review locked accounts and issue one-time recovery keys.</p>
        </div>
        <button type="button" onClick={() => void refresh()} className="inline-flex items-center gap-2 rounded-lg border border-slate-300 dark:border-zinc-700 px-3 py-2 text-sm" aria-label="Refresh authentication controls">
          <RefreshCw size={15} /> Refresh
        </button>
      </div>

      {error && <p role="alert" className="rounded-lg bg-red-50 dark:bg-red-950/30 px-3 py-2 text-sm text-red-700 dark:text-red-300">{error}</p>}

      <div>
        <div className="flex items-center justify-between gap-3 mb-3">
          <div>
            <h3 className="font-semibold">Locked accounts</h3>
            <p className="text-sm text-slate-600 dark:text-zinc-400">After {controls?.login_max_failures ?? 5} failed passwords, an administrator can issue a one-use key valid for {controls?.unlock_key_ttl_minutes ?? 15} minutes.</p>
          </div>
          <span className="rounded-full bg-slate-100 dark:bg-zinc-800 px-2.5 py-1 text-xs">{lockouts.length}</span>
        </div>
        {lockouts.length === 0 ? (
          <p className="rounded-lg bg-slate-50 dark:bg-zinc-900 px-4 py-3 text-sm text-slate-600 dark:text-zinc-400">No accounts are currently locked.</p>
        ) : (
          <ul className="divide-y divide-slate-200 dark:divide-zinc-800 rounded-xl border border-slate-200 dark:border-zinc-800">
            {lockouts.map((account) => (
              <li key={account.id} className="flex flex-wrap items-center justify-between gap-3 p-4">
                <div>
                  <p className="font-medium">{account.name}</p>
                  <p className="text-sm text-slate-600 dark:text-zinc-400">{account.email} · {account.account_type}</p>
                </div>
                <button type="button" disabled={busy !== null} onClick={() => void issueKey(account)} className="inline-flex items-center gap-2 rounded-lg bg-amber-600 px-3 py-2 text-sm font-semibold text-white disabled:opacity-60">
                  <KeyRound size={15} /> {busy === account.id ? 'Issuing…' : 'Issue one-time key'}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {issued && (
        <div className="rounded-xl border border-amber-400/50 bg-amber-50 dark:bg-amber-950/20 p-4" role="status">
          <h3 className="font-semibold">One-time key for {issued.email}</h3>
          <p className="mt-1 text-sm text-slate-700 dark:text-zinc-300">Give this key to the user through your approved private channel. It will not be shown again and expires at {new Date(issued.expires_at).toLocaleString()}.</p>
          <code className="mt-3 block select-all break-all rounded-lg bg-white dark:bg-black/30 p-3 text-base font-bold tracking-wide">{issued.key}</code>
        </div>
      )}
    </section>
  );
};
