import React, { useState, useEffect } from 'react';
import { AlertCircle, ArrowRight, Building2, Eye, EyeOff, Lock, Mail, ShieldCheck, User } from 'lucide-react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { BrandLogo } from '../components/navbar/BrandLogo';
import { LoginVisual } from '../components/auth/LoginVisual';
import { PageMeta } from '../components/ui/PageMeta';
import { useAuth } from '../context/AuthContext';
import { ApiError, apiGetRegistrationStatus } from '../services/api';

// Mirrors the backend UserRegister schema (backend/app/schemas/auth.py).
const PASSWORD_MIN = 8;
const PASSWORD_MAX = 72;
export const LoginPage: React.FC = () => {
  const { user, login, register } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [registrationEnabled, setRegistrationEnabled] = useState(false);
  const [unlockRequired, setUnlockRequired] = useState(false);
  const [unlockKey, setUnlockKey] = useState('');

  useEffect(() => {
    let active = true;
    apiGetRegistrationStatus()
      .then(({ registration_enabled }) => {
        if (!active) return;
        setRegistrationEnabled(registration_enabled);
        if (registration_enabled && searchParams.get('mode') === 'register') setMode('register');
      })
      .catch(() => { if (active) setRegistrationEnabled(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const urlMode = searchParams.get('mode');
    if (urlMode === 'login') setMode('login');
    if (registrationEnabled && urlMode === 'register') setMode('register');
  }, [searchParams, registrationEnabled]);

  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(() =>
    searchParams.get('expired') ? 'Your session has expired. Please sign in again.' : null
  );
  const [loading, setLoading] = useState(false);
  const isRegister = mode === 'register';
  const justCreated = !isRegister && searchParams.get('created') === '1';

  // If already logged in with a real registered account, redirect to workspace
  React.useEffect(() => {
    if (user) {
      navigate('/app', { replace: true });
    }
  }, [user, navigate]);

  const switchMode = (next: 'login' | 'register') => {
    setMode(next);
    setError(null);
    setShowPassword(false);
    setUnlockRequired(false);
    setUnlockKey('');
    // The "Account created" note belongs to the moment after sign-up only.
    if (searchParams.has('created')) navigate(`/login?mode=${next}`, { replace: true });
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      if (mode === 'login') {
        await login(email.trim(), password, unlockKey.trim() || undefined);
        navigate('/app', { replace: true });
      } else {
        if (!name.trim()) throw new Error('Please enter your full name');
        await register(name.trim(), email.trim(), password);
        // A registration-enabled user can start using the chatbot immediately.
        await login(email.trim(), password);
        navigate('/app', { replace: true });
      }
    } catch (err: any) {
      if (err instanceof ApiError && err.status === 423) setUnlockRequired(true);
      setError(err.message || 'Authentication failed. Please verify your credentials.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <PageMeta
        title={isRegister ? 'Create Account' : 'Sign In'}
        description="Continue to your Clyptus.AI knowledge workspace."
      />
      <main className="auth-split">
        <aside className="auth-visual">
          <BrandLogo size="xl" shine />
          <LoginVisual className="auth-visual-art" />
          <figure className="auth-quote">
            <span className="auth-quote-icon" aria-hidden="true">
              <ShieldCheck size={22} strokeWidth={2} />
            </span>
            <blockquote>
              “Your conversations stay private to your account, and every RAG Pipeline answer is checked
              against SAP documentation before you see it.”
            </blockquote>
            <figcaption>Private by default · Verified by design</figcaption>
          </figure>
        </aside>

        <section className="auth-form-side">
          <div className="auth-form-scroll">
            <div className="auth-mobile-brand">
              <BrandLogo />
            </div>

            <div className="login-card">
              <span className="eyebrow">SAP BRIM KNOWLEDGE ASSISTANT</span>
              <h1>{isRegister ? 'Create your account' : 'Welcome back'}</h1>
              <p>{isRegister ? 'Start asking SAP BRIM questions in minutes.' : 'Sign in to your Clyptus.AI workspace.'}</p>

              {registrationEnabled && <div className="auth-tabs" role="tablist" aria-label="Account">
                <button
                  type="button"
                  role="tab"
                  aria-selected={!isRegister}
                  className={!isRegister ? 'active' : ''}
                  onClick={() => switchMode('login')}
                >
                  Sign In
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={isRegister}
                  className={isRegister ? 'active' : ''}
                  onClick={() => switchMode('register')}
                >
                  Create Account
                </button>
              </div>}

              {justCreated && !error && (
                <p className="auth-notice" role="status">
                  Account created. Please sign in.
                </p>
              )}

              {error && (
                <div className="auth-error" role="alert">
                  <AlertCircle className="w-4 h-4 shrink-0" aria-hidden="true" />
                  <span>{error}</span>
                </div>
              )}

              <form onSubmit={handleSubmit} className="auth-form">
                {isRegister && (
                  <div className="auth-field">
                    <label htmlFor="auth-name">Full name</label>
                    <div className="auth-input">
                      <User aria-hidden="true" />
                      <input
                        id="auth-name"
                        type="text"
                        required
                        minLength={2}
                        maxLength={100}
                        autoComplete="name"
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder="Your name"
                      />
                    </div>
                  </div>
                )}

                <div className="auth-field">
                  <label htmlFor="auth-email">Work email</label>
                  <div className="auth-input">
                    <Mail aria-hidden="true" />
                    <input
                      id="auth-email"
                      type="email"
                      required
                      autoComplete={isRegister ? 'email' : 'username'}
                      inputMode="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="name@company.com"
                    />
                  </div>
                </div>

                <div className="auth-field">
                  <label htmlFor="auth-password">Password</label>
                  <div className="auth-input">
                    <Lock aria-hidden="true" />
                    <input
                      id="auth-password"
                      type={showPassword ? 'text' : 'password'}
                      required
                      minLength={isRegister ? PASSWORD_MIN : undefined}
                      maxLength={PASSWORD_MAX}
                      autoComplete={isRegister ? 'new-password' : 'current-password'}
                      aria-describedby={isRegister ? 'auth-password-hint' : undefined}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder={isRegister ? `At least ${PASSWORD_MIN} characters` : 'Your password'}
                    />
                    <button
                      type="button"
                      className="auth-reveal"
                      onClick={() => setShowPassword((v) => !v)}
                      aria-label={showPassword ? 'Hide password' : 'Show password'}
                      aria-pressed={showPassword}
                    >
                      {showPassword ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
                    </button>
                  </div>
                  {isRegister && (
                    <small id="auth-password-hint" className="auth-hint">
                      {PASSWORD_MIN}–{PASSWORD_MAX} characters.
                    </small>
                  )}
                </div>

                {!isRegister && unlockRequired && (
                  <div className="auth-field">
                    <label htmlFor="auth-unlock-key">Admin unlock key</label>
                    <div className="auth-input">
                      <ShieldCheck aria-hidden="true" />
                      <input
                        id="auth-unlock-key"
                        type="text"
                        required
                        autoComplete="one-time-code"
                        value={unlockKey}
                        onChange={(e) => setUnlockKey(e.target.value)}
                        placeholder="Enter the one-time key from your administrator"
                      />
                    </div>
                    <small className="auth-hint">Ask your administrator for a one-time unlock key.</small>
                  </div>
                )}

                <button type="submit" disabled={loading} className="button button-brand auth-submit">
                  <span>{loading ? (isRegister ? 'Creating account…' : 'Signing in…') : isRegister ? 'Create account' : 'Sign in'}</span>
                  <ArrowRight size={15} aria-hidden="true" />
                </button>
              </form>

              {/* Organisations: the Organisation Portal explains the workspace and offers sign-in, activation and requests. */}
              <div className="auth-org">
                <span className="auth-divider">or</span>
                <Link to="/organisation" className="auth-org-button" aria-describedby="auth-org-note">
                  <Building2 size={16} aria-hidden="true" />
                  Create an organisation
                </Link>
                <p id="auth-org-note">Set up a shared Clyptus.AI workspace for your team, or sign in to yours.</p>
              </div>
            </div>

            <div className="login-legal">
              <span>Hybrid retrieval with claim-level verification.</span>
              <Link to="/features">Explore Clyptus.AI</Link>
            </div>
          </div>
        </section>
      </main>
    </>
  );
};
