import React, { useState } from 'react';
import { AlertCircle, ArrowRight, Lock } from 'lucide-react';
import { RevealButton, usePasswordReveal } from '../../components/ui/PasswordReveal';
import { useNavigate } from 'react-router-dom';
import { PageMeta } from '../../components/ui/PageMeta';
import { OrgAuthLayout } from '../../components/organisation/OrgAuthLayout';
import { PASSWORD_MAX, PASSWORD_MIN, validateNewPassword } from '../../components/organisation/orgFormat';
import { useAuth } from '../../context/AuthContext';
import { apiChangePassword } from '../../services/api';
import { postSignInPath, signOutPathFor } from '../../layouts/accountRouting';

/** Forced password change for new members and reset accounts (POST /auth/change-password). */
export const OrgChangePasswordPage: React.FC = () => {
  const { user, applySession, logout } = useAuth();
  const navigate = useNavigate();
  const forced = Boolean(user?.must_change_password);

  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const currentReveal = usePasswordReveal();
  const nextReveal = usePasswordReveal();
  const confirmReveal = usePasswordReveal();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    const problem = !current
      ? 'Enter your current password.'
      : next === current
        ? 'Choose a password that is different from the current one.'
        : validateNewPassword(next, confirm);
    setError(problem);
    if (problem) return;
    setBusy(true);
    try {
      // The server revokes older sessions and returns a new token for this one.
      const updated = applySession(await apiChangePassword(current, next));
      navigate(postSignInPath(updated), { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not change your password.');
      setBusy(false);
    }
  };

  const signOut = () => {
    const destination = signOutPathFor(user);
    logout();
    navigate(destination, { replace: true });
  };


  return (
    <>
      <PageMeta title="Change Password" description="Set a new password for your Clyptus.AI account." />
      <OrgAuthLayout
        backTo={forced ? undefined : postSignInPath({ ...user, must_change_password: false })}
        backLabel={forced ? undefined : 'Back to workspace'}
        quote="“Temporary passwords work only once. Choose your own password to finish setting up your account.”"
        caption="Your password · Known only to you"
      >
        <span className="eyebrow">{user?.organization_name ? user.organization_name.toUpperCase() : 'ACCOUNT SECURITY'}</span>
        <h1>Set a new password</h1>
        <p>
          {forced
            ? 'Your administrator gave you a temporary password. Choose a new one to continue.'
            : 'Choose a new password for your account.'}
        </p>

        {error && (
          <div className="auth-error" role="alert">
            <AlertCircle className="w-4 h-4 shrink-0" aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <form className="auth-form" onSubmit={handleSubmit} noValidate>
          <input type="email" name="username" autoComplete="username" value={user?.email ?? ''} readOnly hidden />
          <div className="auth-field">
            <label htmlFor="org-current-password">{forced ? 'Temporary password' : 'Current password'}</label>
            <div className="auth-input">
              <Lock aria-hidden="true" />
              <input
                id="org-current-password"
                type={currentReveal.type}
                required
                maxLength={PASSWORD_MAX}
                autoComplete="current-password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
              <RevealButton visible={currentReveal.visible} onToggle={currentReveal.toggle} label="current password" />
            </div>
          </div>
          <div className="auth-field">
            <label htmlFor="org-new-password">New password</label>
            <div className="auth-input">
              <Lock aria-hidden="true" />
              <input
                id="org-new-password"
                type={nextReveal.type}
                required
                minLength={PASSWORD_MIN}
                maxLength={PASSWORD_MAX}
                autoComplete="new-password"
                aria-describedby="org-new-password-hint"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                placeholder={`At least ${PASSWORD_MIN} characters`}
              />
              <RevealButton visible={nextReveal.visible} onToggle={nextReveal.toggle} label="new password" />
            </div>
            <small id="org-new-password-hint" className="auth-hint">
              {PASSWORD_MIN}–{PASSWORD_MAX} characters.
            </small>
          </div>
          <div className="auth-field">
            <label htmlFor="org-confirm-password">Confirm new password</label>
            <div className="auth-input">
              <Lock aria-hidden="true" />
              <input
                id="org-confirm-password"
                type={confirmReveal.type}
                required
                minLength={PASSWORD_MIN}
                maxLength={PASSWORD_MAX}
                autoComplete="new-password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
              <RevealButton visible={confirmReveal.visible} onToggle={confirmReveal.toggle} label="confirmation password" />
            </div>
          </div>
          <button type="submit" disabled={busy} className="button button-brand auth-submit">
            <span>{busy ? 'Saving…' : 'Save new password'}</span>
            <ArrowRight size={15} aria-hidden="true" />
          </button>
          <button type="button" className="org-link-btn" onClick={signOut} disabled={busy}>
            Sign out instead
          </button>
        </form>
      </OrgAuthLayout>
    </>
  );
};
