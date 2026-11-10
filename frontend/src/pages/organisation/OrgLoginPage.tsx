import React, { useEffect, useState } from 'react';
import { AlertCircle, ArrowRight, Building2, ChevronRight, Eye, EyeOff, Info, KeyRound, Lock, Mail, UserRound } from 'lucide-react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { PageMeta } from '../../components/ui/PageMeta';
import { OrgAuthLayout } from '../../components/organisation/OrgAuthLayout';
import { useAuth } from '../../context/AuthContext';
import { ORG_ACTIVATE_PATH, isOrgUser, orgSignInDecision, postSignInPath } from '../../layouts/accountRouting';
import { ApiError } from '../../services/api';

/** Organisation sign-in (POST /org/auth/login). Individual accounts are told to use /login. */
export const OrgLoginPage: React.FC = () => {
  const { user, orgLogin } = useAuth();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [unlockKey, setUnlockKey] = useState('');
  const [unlockRequired, setUnlockRequired] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(() =>
    searchParams.get('expired') ? 'Your session has expired. Please sign in again.' : null,
  );

  // A signed-in organisation account goes to its workspace. A signed-in individual stays here: they came
  // to sign in to their organisation, and signing in below switches the session.
  useEffect(() => {
    const decision = orgSignInDecision(user);
    if (decision.kind === 'redirect') navigate(decision.to, { replace: true });
  }, [user, navigate]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const signedIn = await orgLogin(email.trim(), password, unlockKey.trim() || undefined);
      navigate(postSignInPath(signedIn), { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.status === 423) setUnlockRequired(true);
      setError(err instanceof Error ? err.message : 'Sign-in failed. Please check your credentials.');
    } finally {
      setLoading(false);
    }
  };

  const signedInIndividual = user && !isOrgUser(user) ? user : null;

  return (
    <>
      <PageMeta title="Organisation Sign In" description="Sign in to your Clyptus.AI organisation workspace." />
      <OrgAuthLayout>
        <div className="org-auth-heading">
          <span className="org-auth-badge" aria-hidden="true">
            <Building2 size={18} strokeWidth={2} />
          </span>
          <span className="eyebrow">ORGANISATION SIGN-IN</span>
        </div>
        <h1>Sign in to your organisation</h1>
        <p>Use the work email and password from your organisation administrator.</p>

        {signedInIndividual && (
          <div className="org-auth-switch" role="status">
            <Info size={16} aria-hidden="true" />
            <span>
              You&apos;re signed in as <strong>{signedInIndividual.name || signedInIndividual.email}</strong> with an
              individual account. Sign in below to switch to your organisation, or{' '}
              <Link to="/app">go back to your workspace</Link>.
            </span>
          </div>
        )}

        {error && (
          <div className="auth-error" role="alert">
            <AlertCircle className="w-4 h-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} className="auth-form">
          <div className="auth-field">
            <label htmlFor="org-login-email">Work email</label>
            <div className="auth-input">
              <Mail aria-hidden="true" />
              <input
                id="org-login-email"
                type="email"
                required
                autoComplete="username"
                inputMode="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="name@company.com"
              />
            </div>
          </div>

          <div className="auth-field">
            <label htmlFor="org-login-password">Password</label>
            <div className="auth-input">
              <Lock aria-hidden="true" />
              <input
                id="org-login-password"
                type={showPassword ? 'text' : 'password'}
                required
                maxLength={72}
                autoComplete="current-password"
                aria-describedby="org-login-password-hint"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Your password"
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
            <small id="org-login-password-hint" className="auth-hint">
              <strong>First sign-in?</strong> Use the temporary password from your administrator. You&apos;ll choose
              your own straight after.
            </small>
          </div>

          {!unlockRequired ? null : (
            <div className="auth-field">
              <label htmlFor="org-login-unlock-key">Admin unlock key</label>
              <div className="auth-input">
                <KeyRound aria-hidden="true" />
                <input
                  id="org-login-unlock-key"
                  type="text"
                  required
                  autoComplete="one-time-code"
                  value={unlockKey}
                  onChange={(event) => setUnlockKey(event.target.value)}
                  placeholder="Enter the one-time key from your administrator"
                />
              </div>
              <small className="auth-hint">Ask your administrator for a one-time unlock key.</small>
            </div>
          )}

          <button type="submit" disabled={loading} className="button button-brand auth-submit">
            <span>{loading ? 'Signing in…' : 'Sign in to workspace'}</span>
            <ArrowRight size={15} aria-hidden="true" />
          </button>
        </form>

        <nav className="org-auth-exits" aria-label="Other ways in">
          <Link className="org-auth-exit" to={ORG_ACTIVATE_PATH}>
            <KeyRound size={16} aria-hidden="true" />
            <span>
              <strong>Have an activation code?</strong>
              <small>Activate your organisation</small>
            </span>
            <ChevronRight size={15} aria-hidden="true" />
          </Link>
          <Link className="org-auth-exit" to="/login">
            <UserRound size={16} aria-hidden="true" />
            <span>
              <strong>Using Clyptus.AI on your own?</strong>
              <small>Personal sign-in</small>
            </span>
            <ChevronRight size={15} aria-hidden="true" />
          </Link>
        </nav>
      </OrgAuthLayout>
    </>
  );
};
