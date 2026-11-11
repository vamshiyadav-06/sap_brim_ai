import React, { useState } from 'react';
import { AlertCircle, ArrowRight, Lock, User } from 'lucide-react';
import { RevealButton, usePasswordReveal } from '../ui/PasswordReveal';
import type { ActivationCodeDetails } from '../../types';
import { PASSWORD_MAX, PASSWORD_MIN, formatDateTime, validateNewPassword } from './orgFormat';

/** Step 2 of activation: confirm the organisation, then set the administrator's name and password. */
export function ActivationAccountForm({
  details,
  submitting,
  error,
  onSubmit,
  onBack,
}: {
  details: ActivationCodeDetails;
  submitting: boolean;
  error: string | null;
  onSubmit: (name: string, password: string) => void;
  onBack: () => void;
}) {
  const [name, setName] = useState(details.admin_name ?? '');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const passwordReveal = usePasswordReveal();
  const confirmReveal = usePasswordReveal();
  const [localError, setLocalError] = useState<string | null>(null);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) {
      setLocalError('Enter your full name.');
      return;
    }
    const problem = validateNewPassword(password, confirm);
    setLocalError(problem);
    if (!problem) onSubmit(name.trim(), password);
  };

  const shownError = localError ?? error;

  return (
    <>
      <div className="org-activation-summary">
        <dl>
          <div>
            <dt>Organisation</dt>
            <dd>{details.organisation_name}</dd>
          </div>
          <div>
            <dt>Administrator email</dt>
            <dd>{details.admin_email}</dd>
          </div>
          <div>
            <dt>Code expires</dt>
            <dd>{formatDateTime(details.expires_at)}</dd>
          </div>
        </dl>
      </div>

      {shownError && (
        <div className="auth-error" role="alert">
          <AlertCircle className="w-4 h-4 shrink-0" aria-hidden="true" />
          <span>{shownError}</span>
        </div>
      )}

      <form className="auth-form" onSubmit={handleSubmit} noValidate>
        {/* Lets password managers save the new credentials under the administrator email. */}
        <input type="email" name="username" autoComplete="username" value={details.admin_email} readOnly hidden />
        <div className="auth-field">
          <label htmlFor="org-activate-name">Full name</label>
          <div className="auth-input">
            <User aria-hidden="true" />
            <input
              id="org-activate-name"
              type="text"
              required
              minLength={2}
              maxLength={120}
              autoComplete="name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Your name"
            />
          </div>
        </div>
        <div className="auth-field">
          <label htmlFor="org-activate-password">Password</label>
          <div className="auth-input">
            <Lock aria-hidden="true" />
            <input
              id="org-activate-password"
              type={passwordReveal.type}
              required
              minLength={PASSWORD_MIN}
              maxLength={PASSWORD_MAX}
              autoComplete="new-password"
              aria-describedby="org-activate-password-hint"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={`At least ${PASSWORD_MIN} characters`}
            />
            <RevealButton visible={passwordReveal.visible} onToggle={passwordReveal.toggle} label="password" />
          </div>
          <small id="org-activate-password-hint" className="auth-hint">
            {PASSWORD_MIN}–{PASSWORD_MAX} characters.
          </small>
        </div>
        <div className="auth-field">
          <label htmlFor="org-activate-confirm">Confirm password</label>
          <div className="auth-input">
            <Lock aria-hidden="true" />
            <input
              id="org-activate-confirm"
              type={confirmReveal.type}
              required
              minLength={PASSWORD_MIN}
              maxLength={PASSWORD_MAX}
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              placeholder="Repeat the password"
            />
            <RevealButton visible={confirmReveal.visible} onToggle={confirmReveal.toggle} label="confirmation password" />
          </div>
        </div>
        <button type="submit" disabled={submitting} className="button button-brand auth-submit">
          <span>{submitting ? 'Activating…' : 'Activate organisation'}</span>
          <ArrowRight size={15} aria-hidden="true" />
        </button>
        <button type="button" className="org-link-btn" onClick={onBack} disabled={submitting}>
          Use a different code
        </button>
      </form>
    </>
  );
}
