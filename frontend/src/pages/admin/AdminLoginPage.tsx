import React, { useState, useEffect } from 'react';
import { ArrowLeft, ShieldCheck, Mail, Lock, AlertCircle } from 'lucide-react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { PageMeta } from '../../components/ui/PageMeta';
import { useAuth } from '../../context/AuthContext';
import iconOnly from '../../assets/clyptusap-icon.png';
import { ApiError } from '../../services/api';
import { RevealButton, usePasswordReveal } from '../../components/ui/PasswordReveal';

export const AdminLoginPage: React.FC = () => {
  const { user, token, adminLogin } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const reveal = usePasswordReveal();
  const [unlockKey, setUnlockKey] = useState('');
  const [unlockRequired, setUnlockRequired] = useState(false);
  const [error, setError] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get('expired') ? 'Your session has expired. Please sign in again.' : null
  );
  const [loading, setLoading] = useState(false);

  // If already logged in as admin, redirect to /admin
  useEffect(() => {
    if (user?.is_admin && token) {
      const target = (location.state as any)?.from?.pathname || '/admin';
      navigate(target, { replace: true });
    }
  }, [user, token, navigate, location]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      if (!email.trim()) throw new Error('Please enter administrator email address');
      if (!password) throw new Error('Please enter administrator password');

      await adminLogin(email.trim(), password, unlockKey.trim() || undefined);
      navigate('/admin', { replace: true });
    } catch (err: any) {
      if (err instanceof ApiError && err.status === 423) setUnlockRequired(true);
      setError(
        err.message ||
        'Authentication failed: Please verify your administrator credentials or check system permissions.'
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <PageMeta
        title="Super Admin Security Gateway"
        description="Restricted enterprise management gateway for Clyptus.AI SAP knowledge base and document pipelines."
      />

      <div className="min-h-screen bg-white dark:bg-[#06070b] text-slate-900 dark:text-zinc-100 flex flex-col justify-between relative overflow-hidden font-sans">
        {/* Ambient background glows */}
        <div className="absolute -top-40 -left-40 w-96 h-96 bg-amber-500/10 rounded-full blur-3xl pointer-events-none" />
        <div className="absolute top-1/2 -right-40 w-96 h-96 bg-orange-600/10 rounded-full blur-3xl pointer-events-none" />
        <div className="absolute -bottom-40 left-1/3 w-96 h-96 bg-emerald-500/5 rounded-full blur-3xl pointer-events-none" />

        {/* Top Header */}
        <header className="relative z-10 w-full max-w-6xl mx-auto px-6 py-6 flex items-center justify-between">
          <Link
            to="/app"
            className="inline-flex items-center gap-2 text-xs font-medium text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-white transition"
          >
            <ArrowLeft size={14} /> Back to User Workspace
          </Link>

          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-amber-500/10 border border-amber-500/20 text-[11px] font-mono text-amber-700 dark:text-amber-400">
            <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
            <span>SECURE RESTRICTED ZONE</span>
          </div>
        </header>

        {/* Center Container */}
        <main className="relative z-10 flex-1 flex items-center justify-center px-4 py-8">
          <div className="w-full max-w-md bg-white/90 dark:bg-[#0c0e14]/90 backdrop-blur-xl border border-slate-200 dark:border-zinc-800 hover:border-slate-200/80 dark:hover:border-zinc-700/80 transition-all rounded-3xl p-8 sm:p-10 shadow-2xl shadow-slate-200/60 dark:shadow-black/80">
            {/* Header / Avatar */}
            <div className="text-center space-y-3 mb-8">
              <div className="relative inline-block">
                <div className="w-16 h-16 rounded-2xl bg-gradient-to-tr from-amber-600 via-orange-500 to-amber-400 p-0.5 shadow-xl shadow-amber-500/20 mx-auto">
                  <div className="w-full h-full bg-white dark:bg-[#0d0f17] rounded-2xl flex items-center justify-center p-2.5">
                    <img src={iconOnly} alt="Clyptus.AI" className="w-full h-full object-contain" />
                  </div>
                </div>
                <div className="absolute -bottom-1 -right-1 p-1 rounded-full bg-amber-500 text-slate-950 shadow-md">
                  <ShieldCheck size={14} strokeWidth={2.5} />
                </div>
              </div>

              <div>
                <span className="text-[11px] font-mono font-bold tracking-widest text-amber-700 dark:text-amber-400 uppercase">
                  Enterprise Control Center
                </span>
                <h1 className="text-2xl font-bold tracking-tight text-slate-900 dark:text-white mt-1">
                  Super Admin Portal
                </h1>
                <p className="text-xs text-slate-600 dark:text-zinc-400 mt-1 max-w-xs mx-auto leading-relaxed">
                  Authentication required to manage documents, inspect technical sources, and configure RAG pipelines.
                </p>
              </div>
            </div>

            {/* Error message */}
            {error && (
              <div className="mb-5 p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-xs text-rose-700 dark:text-rose-300 flex items-start gap-2.5">
                <AlertCircle size={15} className="text-rose-700 dark:text-rose-400 shrink-0 mt-0.5" />
                <span className="leading-relaxed">{error}</span>
              </div>
            )}

            {/* Login Form */}
            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 dark:text-zinc-300 mb-1.5">
                  Administrator Email
                </label>
                <div className="relative">
                  <Mail className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-500 dark:text-zinc-500 w-4 h-4 pointer-events-none" />
                  <input
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="admin@company.com"
                    autoComplete="username"
                    className="w-full bg-white dark:bg-[#12151f] border border-slate-200/80 dark:border-zinc-700/80 focus:border-amber-500 text-slate-900 dark:text-white rounded-xl pl-10 pr-3.5 py-2.5 text-xs focus:outline-none focus:ring-1 focus:ring-amber-500 transition font-mono"
                  />
                </div>
              </div>

              <div>
                <label className="block text-xs font-semibold text-slate-700 dark:text-zinc-300 mb-1.5">
                  Master Password
                </label>
                <div className="relative">
                  <Lock className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-500 dark:text-zinc-500 w-4 h-4 pointer-events-none" />
                  <input
                    type={reveal.type}
                    required
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="Enter admin password"
                    autoComplete="current-password"
                    className="w-full bg-white dark:bg-[#12151f] border border-slate-200/80 dark:border-zinc-700/80 focus:border-amber-500 text-slate-900 dark:text-white rounded-xl pl-10 pr-11 py-2.5 text-xs focus:outline-none focus:ring-1 focus:ring-amber-500 transition font-mono tracking-wider"
                  />
                  <RevealButton
                    visible={reveal.visible}
                    onToggle={reveal.toggle}
                    label="password"
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 w-8 h-8 grid place-items-center rounded-lg text-slate-500 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-100 dark:hover:bg-zinc-800 transition"
                  />
                </div>
              </div>

              {unlockRequired && (
                <div>
                  <label htmlFor="admin-unlock-key" className="block text-xs font-semibold text-slate-700 dark:text-zinc-300 mb-1.5">
                    One-time administrator unlock key
                  </label>
                  <input
                    id="admin-unlock-key"
                    type="text"
                    required
                    autoComplete="one-time-code"
                    value={unlockKey}
                    onChange={(event) => setUnlockKey(event.target.value)}
                    placeholder="Ask another administrator for the key"
                    className="w-full bg-white dark:bg-[#12151f] border border-slate-200/80 dark:border-zinc-700/80 focus:border-amber-500 text-slate-900 dark:text-white rounded-xl px-3.5 py-2.5 text-xs focus:outline-none focus:ring-1 focus:ring-amber-500 transition font-mono"
                  />
                </div>
              )}

              <div className="pt-2">
                <button
                  type="submit"
                  disabled={loading}
                  className="w-full py-3 px-4 rounded-xl font-bold text-xs bg-gradient-to-r from-amber-500 via-orange-500 to-amber-600 hover:from-amber-400 hover:to-amber-500 text-slate-950 shadow-lg shadow-amber-500/25 hover:shadow-amber-500/40 transition active:scale-[0.99] disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer flex items-center justify-center gap-2"
                >
                  {loading ? (
                    <>
                      <div className="w-4 h-4 border-2 border-slate-200/20 dark:border-slate-950/20 border-t-slate-950 rounded-full animate-spin" />
                      <span>Authenticating Administrator...</span>
                    </>
                  ) : (
                    <>
                      <ShieldCheck size={16} strokeWidth={2.5} />
                      <span>Unlock Super Admin Dashboard</span>
                    </>
                  )}
                </button>
              </div>
            </form>

            <div className="mt-6 pt-5 border-t border-slate-200/80 dark:border-zinc-800/80 text-center">
              <p className="text-[11px] text-slate-500 dark:text-zinc-500">
                Regular users must use standard authentication.{' '}
                <Link to="/login" className="text-amber-700 dark:text-amber-400 hover:underline font-medium">
                  User Login &rarr;
                </Link>
              </p>
            </div>
          </div>
        </main>

        {/* Footer */}
        <footer className="relative z-10 w-full max-w-6xl mx-auto px-6 py-6 text-center text-[11px] text-slate-500 dark:text-zinc-500">
          Clyptus.AI Enterprise Security · High-Security Administrator Environment
        </footer>
      </div>
    </>
  );
};
