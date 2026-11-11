import React, { useCallback, useEffect, useState } from 'react';
import { Building2, Eye, Plus, RefreshCw } from 'lucide-react';
import type { AdminOrganisation, OrganisationStatus } from '../../types';
import {
  apiAdminApproveOrganisation,
  apiAdminCreateOrganisation,
  apiAdminListOrganisations,
  apiAdminReissueActivationCode,
  apiAdminRejectOrganisation,
  apiAdminSetOrganisationStatus,
} from '../../services/api';
import { ConfirmDialog, OrgModal } from '../organisation/OrgDialog';
import { OneTimeSecret } from '../organisation/OneTimeSecret';
import { ORG_STATUS_LABELS, formatDate, formatDateTime, isValidEmail } from '../organisation/orgFormat';
import { OrganisationDetailPanel } from './OrganisationDetailPanel';
import { useRefreshSpinner } from '../ui/useRefreshSpinner';
import '../organisation/organisation.css';

type StatusFilter = OrganisationStatus | '';
type TargetStatus = 'active' | 'suspended' | 'deactivated';

const FILTERS: Array<{ value: StatusFilter; label: string }> = [
  { value: '', label: 'All' },
  { value: 'requested', label: 'Requested' },
  { value: 'pending_activation', label: 'Pending activation' },
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
  { value: 'deactivated', label: 'Deactivated' },
  { value: 'rejected', label: 'Rejected' },
];

const STATUS_TONE: Record<OrganisationStatus, string> = {
  requested: 'org-badge-brand',
  pending_activation: 'org-badge-warn',
  active: 'org-badge-active',
  suspended: 'org-badge-warn',
  deactivated: 'org-badge-danger',
  rejected: 'org-badge-danger',
};

type Pending =
  | { kind: 'reject'; org: AdminOrganisation }
  | { kind: 'reissue'; org: AdminOrganisation }
  | { kind: 'status'; org: AdminOrganisation; status: TargetStatus };

interface IssuedCode {
  organisationName: string;
  adminEmail?: string;
  code: string;
  expiresAt: string;
}

const errorText = (err: unknown, fallback: string) => (err instanceof Error ? err.message : fallback);

const STATUS_ACTION: Record<TargetStatus, { label: string; verb: string; body: string; tone: 'danger' | 'default' }> = {
  active: {
    label: 'Reactivate',
    verb: 'Reactivate organisation',
    body: 'Members can sign in again with their existing credentials.',
    tone: 'default',
  },
  suspended: {
    label: 'Suspend',
    verb: 'Suspend organisation',
    body: 'Every member is signed out and cannot sign in until the organisation is reactivated. Data is kept.',
    tone: 'danger',
  },
  deactivated: {
    label: 'Deactivate',
    verb: 'Deactivate organisation',
    body: 'Every member is signed out and the workspace is closed. Use this when the organisation leaves Clyptus.',
    tone: 'danger',
  },
};

/** Super Admin: Organisation Management (list, create, approve/reject, codes, status, detail). */
export function OrganisationsAdminPanel() {
  const [filter, setFilter] = useState<StatusFilter>('');
  const [orgs, setOrgs] = useState<AdminOrganisation[] | null>(null);
  const [loadedFilter, setLoadedFilter] = useState<StatusFilter | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);
  const [creating, setCreating] = useState(false);
  const [approving, setApproving] = useState<AdminOrganisation | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingBusy, setPendingBusy] = useState(false);
  const [pendingError, setPendingError] = useState<string | null>(null);
  const [issued, setIssued] = useState<IssuedCode | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const spinner = useRefreshSpinner();

  useEffect(() => {
    let cancelled = false;
    apiAdminListOrganisations(filter)
      .then((data) => {
        if (cancelled) return;
        setOrgs(data);
        setLoadedFilter(filter);
        setError(null);
        spinner.stop();
      })
      .catch((err) => {
        if (cancelled) return;
        setError(errorText(err, 'Could not load organisations.'));
        spinner.stop();
      });
    return () => {
      cancelled = true;
    };
  }, [filter, reloadCount]);

  const reload = useCallback(() => setReloadCount((n) => n + 1), []);
  const loading = loadedFilter !== filter && !error;

  const confirmPending = async () => {
    if (!pending) return;
    setPendingBusy(true);
    setPendingError(null);
    const { org } = pending;
    try {
      if (pending.kind === 'reject') {
        await apiAdminRejectOrganisation(org.id);
        setNotice(`The request from ${org.name} was rejected.`);
      } else if (pending.kind === 'reissue') {
        const result = await apiAdminReissueActivationCode(org.id);
        setIssued({
          organisationName: org.name,
          adminEmail: org.active_code?.admin_email,
          code: result.activation_code,
          expiresAt: result.expires_at,
        });
      } else {
        await apiAdminSetOrganisationStatus(org.id, pending.status);
        setNotice(`${org.name} is now ${ORG_STATUS_LABELS[pending.status].toLowerCase()}.`);
      }
      setPending(null);
      reload();
    } catch (err) {
      setPendingError(errorText(err, 'That did not work. Please try again.'));
    } finally {
      setPendingBusy(false);
    }
  };

  const openPending = (next: Pending) => {
    setPendingError(null);
    setNotice(null);
    setPending(next);
  };

  const actionsFor = (org: AdminOrganisation) => {
    const buttons: React.ReactNode[] = [];
    if (org.status === 'requested') {
      buttons.push(
        <button key="approve" type="button" className="org-btn org-btn-sm org-btn-primary" onClick={() => setApproving(org)}>
          Approve
        </button>,
        <button key="reject" type="button" className="org-btn org-btn-sm org-btn-danger" onClick={() => openPending({ kind: 'reject', org })}>
          Reject
        </button>,
      );
    }
    if (org.status === 'pending_activation') {
      buttons.push(
        <button key="reissue" type="button" className="org-btn org-btn-sm" onClick={() => openPending({ kind: 'reissue', org })}>
          Re-issue code
        </button>,
      );
    }
    if (org.status === 'active') {
      buttons.push(
        <button key="suspend" type="button" className="org-btn org-btn-sm" onClick={() => openPending({ kind: 'status', org, status: 'suspended' })}>
          Suspend
        </button>,
      );
    }
    if (org.status === 'suspended' || org.status === 'deactivated') {
      buttons.push(
        <button key="reactivate" type="button" className="org-btn org-btn-sm" onClick={() => openPending({ kind: 'status', org, status: 'active' })}>
          Reactivate
        </button>,
      );
    }
    if (org.status === 'active' || org.status === 'suspended') {
      buttons.push(
        <button
          key="deactivate"
          type="button"
          className="org-btn org-btn-sm org-btn-danger"
          onClick={() => openPending({ kind: 'status', org, status: 'deactivated' })}
        >
          Deactivate
        </button>,
      );
    }
    return buttons;
  };

  const pendingCopy = (() => {
    if (!pending) return null;
    if (pending.kind === 'reject') {
      return {
        title: `Reject the request from ${pending.org.name}?`,
        body: 'The organisation is marked as rejected. The contact is not notified automatically.',
        confirm: 'Reject request',
        tone: 'danger' as const,
      };
    }
    if (pending.kind === 'reissue') {
      return {
        title: `Issue a new activation code for ${pending.org.name}?`,
        body: 'Any unused code for this organisation stops working. The new code is shown once.',
        confirm: 'Issue new code',
        tone: 'default' as const,
      };
    }
    const action = STATUS_ACTION[pending.status];
    return { title: `${action.verb}: ${pending.org.name}?`, body: action.body, confirm: action.verb, tone: action.tone };
  })();

  return (
    <section className="org-console org-panel" aria-labelledby="admin-orgs-title" style={{ marginTop: 0 }}>
      <div className="org-panel-head">
        <div>
          <h2 id="admin-orgs-title">
            <Building2 size={18} aria-hidden="true" style={{ display: 'inline', verticalAlign: '-3px', marginRight: 8 }} />
            Organisation Management
          </h2>
          <p>Organisations are created only here. Activation codes are single-use, time-limited and shown once.</p>
        </div>
        <div className="org-app-actions">
          <button
            type="button"
            className="org-btn"
            disabled={spinner.spinning}
            aria-busy={spinner.spinning}
            onClick={() => { spinner.start(); reload(); }}
          >
            <RefreshCw size={14} aria-hidden="true" className={spinner.spinning ? 'animate-spin' : ''} /> Refresh
          </button>
          <button type="button" className="org-btn org-btn-primary" onClick={() => setCreating(true)}>
            <Plus size={15} aria-hidden="true" /> Create organisation
          </button>
        </div>
      </div>

      <div className="org-filter-pills" role="group" aria-label="Filter by status" style={{ marginBottom: 14 }}>
        {FILTERS.map(({ value, label }) => (
          <button key={value || 'all'} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
      </div>

      {notice && (
        <p className="org-notice" role="status">
          {notice}
        </p>
      )}
      {error && (
        <p className="org-alert" role="alert">
          {error}
        </p>
      )}
      {loading && (
        <p className="org-form-note" role="status">
          Loading organisations…
        </p>
      )}

      {orgs && !loading && (
        <div className="org-table-wrap">
          <table className="org-table">
            <thead>
              <tr>
                <th scope="col">Organisation</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">Members</th>
                <th scope="col">Created</th>
                <th scope="col">Activation code</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {orgs.length === 0 && (
                <tr>
                  <td colSpan={6}>No organisations{filter ? ` with status “${ORG_STATUS_LABELS[filter]}”` : ''} yet.</td>
                </tr>
              )}
              {orgs.map((org) => (
                <tr key={org.id}>
                  <td>
                    <strong>{org.name}</strong>
                    <small>
                      {org.contact_name ? `${org.contact_name} · ` : ''}
                      {org.contact_email}
                    </small>
                  </td>
                  <td>
                    <span className={`org-badge ${STATUS_TONE[org.status] ?? ''}`}>{ORG_STATUS_LABELS[org.status] ?? org.status}</span>
                  </td>
                  <td className="num">{org.member_count}</td>
                  <td>{formatDate(org.created_at)}</td>
                  <td>
                    {org.active_code ? (
                      <>
                        <strong>••••{org.active_code.hint}</strong>
                        <small>
                          For {org.active_code.admin_email} · expires {formatDateTime(org.active_code.expires_at)}
                        </small>
                      </>
                    ) : (
                      <small>—</small>
                    )}
                  </td>
                  <td>
                    <div className="org-row-actions">
                      <button
                        type="button"
                        className="org-btn org-btn-sm org-btn-ghost"
                        aria-expanded={detailId === org.id}
                        aria-controls="admin-org-detail"
                        onClick={() => setDetailId((id) => (id === org.id ? null : org.id))}
                      >
                        <Eye size={13} aria-hidden="true" /> {detailId === org.id ? 'Hide' : 'Details'}
                      </button>
                      {actionsFor(org)}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detailId && <OrganisationDetailPanel key={detailId} organisationId={detailId} onClose={() => setDetailId(null)} />}

      {creating && (
        <OrganisationFormModal
          mode="create"
          onClose={() => setCreating(false)}
          onIssued={(result) => {
            setCreating(false);
            setIssued(result);
            reload();
          }}
        />
      )}

      {approving && (
        <OrganisationFormModal
          mode="approve"
          organisation={approving}
          onClose={() => setApproving(null)}
          onIssued={(result) => {
            setApproving(null);
            setIssued(result);
            reload();
          }}
        />
      )}

      {pending && pendingCopy && (
        <ConfirmDialog
          title={pendingCopy.title}
          message={<p>{pendingCopy.body}</p>}
          confirmLabel={pendingCopy.confirm}
          tone={pendingCopy.tone}
          busy={pendingBusy}
          error={pendingError}
          onConfirm={confirmPending}
          onCancel={() => setPending(null)}
        />
      )}

      {issued && (
        <div className="org-console">
          <OrgModal
            title="Activation code issued"
            description={
              <>
                Send this code to {issued.adminEmail ? <strong>{issued.adminEmail}</strong> : 'the organisation administrator'} through
                a secure channel. They activate <strong>{issued.organisationName}</strong> at <strong>/organisation/activate</strong>.
              </>
            }
            onClose={() => setIssued(null)}
          >
            <OneTimeSecret label="Activation code" value={issued.code} expiresAt={issued.expiresAt} />
            <div className="org-modal-actions">
              <button type="button" className="org-btn org-btn-primary" onClick={() => setIssued(null)}>
                I have copied it
              </button>
            </div>
          </OrgModal>
        </div>
      )}
    </section>
  );
}

function OrganisationFormModal({
  mode,
  organisation,
  onClose,
  onIssued,
}: {
  mode: 'create' | 'approve';
  organisation?: AdminOrganisation;
  onClose: () => void;
  onIssued: (issued: IssuedCode) => void;
}) {
  const [name, setName] = useState(organisation?.name ?? '');
  const [contactEmail, setContactEmail] = useState(organisation?.contact_email ?? '');
  const [contactName, setContactName] = useState('');
  const [adminName, setAdminName] = useState(organisation?.contact_name ?? '');
  const [adminEmail, setAdminEmail] = useState(organisation?.contact_email ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (mode === 'create' && !name.trim()) return setError('Enter the organisation name.');
    if (mode === 'create' && !isValidEmail(contactEmail)) return setError('Enter a valid contact email.');
    if (!adminName.trim()) return setError('Enter the administrator’s name.');
    if (!isValidEmail(adminEmail)) return setError('Enter a valid administrator email.');
    setBusy(true);
    setError(null);
    try {
      const result =
        mode === 'create'
          ? await apiAdminCreateOrganisation({
              name: name.trim(),
              contact_email: contactEmail.trim(),
              ...(contactName.trim() ? { contact_name: contactName.trim() } : {}),
              admin_name: adminName.trim(),
              admin_email: adminEmail.trim(),
            })
          : await apiAdminApproveOrganisation(organisation!.id, {
              admin_name: adminName.trim(),
              admin_email: adminEmail.trim(),
            });
      onIssued({
        organisationName: result.organisation?.name ?? (name.trim() || organisation?.name || 'the organisation'),
        adminEmail: adminEmail.trim(),
        code: result.activation_code,
        expiresAt: result.expires_at,
      });
    } catch (err) {
      setError(errorText(err, mode === 'create' ? 'Could not create the organisation.' : 'Could not approve the request.'));
      setBusy(false);
    }
  };

  return (
    <div className="org-console">
      <OrgModal
        title={mode === 'create' ? 'Create an organisation' : `Approve ${organisation?.name}`}
        description={
          mode === 'create'
            ? 'The organisation starts as pending activation. Its administrator activates it with the code shown next.'
            : 'Confirm who will administer this organisation. An activation code is issued for them.'
        }
        onClose={busy ? () => undefined : onClose}
        wide
      >
        <form className="org-form" onSubmit={submit} noValidate>
          {error && (
            <p className="org-alert" role="alert">
              {error}
            </p>
          )}
          {mode === 'approve' && organisation?.request_message && (
            <div className="org-field">
              <span className="org-section-title" style={{ margin: 0 }}>
                Request message
              </span>
              <p className="org-form-note" style={{ whiteSpace: 'pre-wrap' }}>
                {organisation.request_message}
              </p>
            </div>
          )}
          {mode === 'create' && (
            <div className="org-form-row">
              <div className="org-field">
                <label htmlFor="admin-org-name">Organisation name</label>
                <input
                  id="admin-org-name"
                  className="org-input"
                  type="text"
                  autoComplete="off"
                  maxLength={120}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
              <div className="org-field">
                <label htmlFor="admin-org-contact">Contact email</label>
                <input
                  id="admin-org-contact"
                  className="org-input"
                  type="email"
                  autoComplete="off"
                  maxLength={255}
                  value={contactEmail}
                  onChange={(e) => setContactEmail(e.target.value)}
                />
              </div>
              <div className="org-field">
                <label htmlFor="admin-org-contact-name">
                  Contact name <span className="org-form-note">(optional)</span>
                </label>
                <input
                  id="admin-org-contact-name"
                  className="org-input"
                  type="text"
                  autoComplete="off"
                  maxLength={120}
                  value={contactName}
                  onChange={(e) => setContactName(e.target.value)}
                />
              </div>
            </div>
          )}
          <div className="org-form-row">
            <div className="org-field">
              <label htmlFor="admin-org-admin-name">Administrator name</label>
              <input
                id="admin-org-admin-name"
                className="org-input"
                type="text"
                autoComplete="off"
                maxLength={120}
                value={adminName}
                onChange={(e) => setAdminName(e.target.value)}
              />
            </div>
            <div className="org-field">
              <label htmlFor="admin-org-admin-email">Administrator email</label>
              <input
                id="admin-org-admin-email"
                className="org-input"
                type="email"
                autoComplete="off"
                maxLength={255}
                value={adminEmail}
                onChange={(e) => setAdminEmail(e.target.value)}
              />
            </div>
          </div>
          <div className="org-modal-actions">
            <button type="button" className="org-btn org-btn-ghost" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="org-btn org-btn-primary" disabled={busy}>
              {busy ? 'Issuing code…' : mode === 'create' ? 'Create and issue code' : 'Approve and issue code'}
            </button>
          </div>
        </form>
      </OrgModal>
    </div>
  );
}
