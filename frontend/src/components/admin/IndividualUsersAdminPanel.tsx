import React, { useEffect, useState } from 'react';
import { ChevronLeft, ChevronRight, Plus, RefreshCw, Search, UserRound } from 'lucide-react';
import type { IndividualUser, IndividualUsersResponse } from '../../types';
import { apiAdminCreateIndividualUser, apiAdminListIndividualUsers, apiAdminSetUserStatus } from '../../services/api';
import { ConfirmDialog } from '../organisation/OrgDialog';
import { formatDate, formatDateTime } from '../organisation/orgFormat';
import '../organisation/organisation.css';

const PAGE_SIZE = 25;

/** Super Admin: Individual User Management (search, paging, activate/deactivate). */
export function IndividualUsersAdminPanel({ currentUserId }: { currentUserId?: string }) {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<(IndividualUsersResponse & { key: string }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<IndividualUser | null>(null);
  const [confirmError, setConfirmError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: '', email: '', password: '' });
  const [createError, setCreateError] = useState<string | null>(null);
  const [createBusy, setCreateBusy] = useState(false);

  const createUser = async (event: React.FormEvent) => {
    event.preventDefault();
    setCreateError(null);
    if (form.name.trim().length < 2) return setCreateError('Enter the user’s name (at least 2 characters).');
    if (!/^\S+@\S+\.\S+$/.test(form.email.trim())) return setCreateError('Enter a valid email address.');
    if (form.password.length < 8) return setCreateError('The password needs at least 8 characters.');
    setCreateBusy(true);
    try {
      const created = await apiAdminCreateIndividualUser({
        name: form.name.trim(),
        email: form.email.trim(),
        password: form.password,
      });
      setNotice(`${created.name} was created and can sign in with the password you set.`);
      setForm({ name: '', email: '', password: '' });
      setCreating(false);
      setSearch('');
      setReloadCount((n) => n + 1);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : 'Could not create the user.');
    } finally {
      setCreateBusy(false);
    }
  };

  const key = `${query}\u0000${offset}`;

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQuery(search.trim());
      setOffset(0);
    }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    let cancelled = false;
    const requestKey = `${query}\u0000${offset}`;
    apiAdminListIndividualUsers({ search: query || undefined, limit: PAGE_SIZE, offset })
      .then((res) => {
        if (cancelled) return;
        setData({ ...res, key: requestKey });
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Could not load users.');
      });
    return () => {
      cancelled = true;
    };
  }, [query, offset, reloadCount]);

  const loading = (!data || data.key !== key) && !error;
  const users = data?.users ?? [];
  const total = data?.total ?? 0;

  const setStatus = async (target: IndividualUser, isActive: boolean) => {
    setBusyId(target.id);
    setConfirmError(null);
    setNotice(null);
    try {
      const updated = await apiAdminSetUserStatus(target.id, isActive);
      setData((prev) =>
        prev
          ? {
              ...prev,
              users: prev.users.map((u) => (u.id === target.id ? { ...u, ...(updated ?? {}), is_active: isActive } : u)),
            }
          : prev,
      );
      setNotice(isActive ? `${target.name} can sign in again.` : `${target.name} has been deactivated and signed out.`);
      setConfirming(null);
    } catch (err) {
      const text = err instanceof Error ? err.message : 'Could not change the account status.';
      if (isActive) setError(text);
      else setConfirmError(text);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="org-console org-panel" aria-labelledby="admin-users-title" style={{ marginTop: 0 }}>
      <div className="org-panel-head">
        <div>
          <h2 id="admin-users-title">
            <UserRound size={18} aria-hidden="true" style={{ display: 'inline', verticalAlign: '-3px', marginRight: 8 }} />
            Individual User Management
          </h2>
          <p>Individual accounts only. Organisation members are managed by their organisation administrators.</p>
        </div>
        <div className="org-app-actions">
          <button type="button" className="org-btn" onClick={() => setReloadCount((n) => n + 1)}>
            <RefreshCw size={14} aria-hidden="true" /> Refresh
          </button>
          <button type="button" className="org-btn org-btn-primary" aria-expanded={creating} onClick={() => setCreating((v) => !v)}>
            <Plus size={14} aria-hidden="true" /> Create user
          </button>
        </div>
      </div>

      {creating && (
        <form className="org-toolbar org-inline-form" style={{ marginBottom: 14 }} onSubmit={createUser} noValidate>
          <input className="org-input" aria-label="New user name" placeholder="Full name" value={form.name} maxLength={100}
            onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <input className="org-input" aria-label="New user email" type="email" placeholder="Email" value={form.email} autoComplete="off"
            onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <input className="org-input" aria-label="New user password" type="password" placeholder="Password (8+ characters)" value={form.password}
            autoComplete="new-password" maxLength={72} onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <button type="submit" className="org-btn org-btn-primary" disabled={createBusy}>
            {createBusy ? 'Creating…' : 'Create account'}
          </button>
          {createError && (
            <p className="org-alert" role="alert" style={{ flexBasis: '100%' }}>
              {createError}
            </p>
          )}
        </form>
      )}

      <div className="org-toolbar" style={{ marginBottom: 14 }}>
        <div className="org-search">
          <Search size={16} aria-hidden="true" />
          <label htmlFor="admin-users-search" className="visually-hidden">
            Search individual users by name or email
          </label>
          <input
            id="admin-users-search"
            type="search"
            autoComplete="off"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by name or email"
          />
        </div>
        <span className="org-result-count" role="status">
          {loading ? 'Loading…' : `${total} ${total === 1 ? 'user' : 'users'}`}
        </span>
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

      {data && (
        <div className="org-table-wrap" aria-busy={loading}>
          <table className="org-table">
            <thead>
              <tr>
                <th scope="col">User</th>
                <th scope="col">Status</th>
                <th scope="col">Joined</th>
                <th scope="col">Last sign-in</th>
                <th scope="col" className="num">Chats</th>
                <th scope="col" className="num">Questions</th>
                <th scope="col">
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {users.length === 0 && (
                <tr>
                  <td colSpan={7}>{query ? 'No users match this search.' : 'No individual users yet.'}</td>
                </tr>
              )}
              {users.map((u) => {
                const isSelf = u.id === currentUserId;
                return (
                  <tr key={u.id}>
                    <td>
                      <strong>
                        {u.name}
                        {isSelf ? ' (you)' : ''}
                      </strong>
                      <small>{u.email}</small>
                    </td>
                    <td>
                      <span className={`org-badge ${u.is_active ? 'org-badge-active' : 'org-badge-danger'}`}>
                        {u.is_active ? 'Active' : 'Deactivated'}
                      </span>{' '}
                      {u.is_admin && <span className="org-badge org-badge-brand">Super Admin</span>}
                    </td>
                    <td>{formatDate(u.created_at)}</td>
                    <td>{u.last_login_at ? formatDateTime(u.last_login_at) : 'Never'}</td>
                    <td className="num">{u.chats}</td>
                    <td className="num">{u.questions}</td>
                    <td>
                      <div className="org-row-actions">
                        {u.is_active ? (
                          <button
                            type="button"
                            className="org-btn org-btn-sm org-btn-danger"
                            disabled={isSelf || busyId === u.id}
                            title={isSelf ? 'You cannot deactivate your own account' : undefined}
                            onClick={() => {
                              setConfirmError(null);
                              setConfirming(u);
                            }}
                          >
                            Deactivate
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="org-btn org-btn-sm"
                            disabled={busyId === u.id}
                            onClick={() => setStatus(u, true)}
                          >
                            Activate
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {total > PAGE_SIZE && (
        <div className="org-pagination">
          <span>
            Showing {total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <div className="org-row-actions">
            <button
              type="button"
              className="org-btn org-btn-sm"
              disabled={offset === 0 || loading}
              onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}
            >
              <ChevronLeft size={14} aria-hidden="true" /> Previous
            </button>
            <button
              type="button"
              className="org-btn org-btn-sm"
              disabled={offset + PAGE_SIZE >= total || loading}
              onClick={() => setOffset((o) => o + PAGE_SIZE)}
            >
              Next <ChevronRight size={14} aria-hidden="true" />
            </button>
          </div>
        </div>
      )}

      {confirming && (
        <ConfirmDialog
          title={`Deactivate ${confirming.name}?`}
          message={<p>They are signed out at once and cannot sign in until an administrator activates the account again.</p>}
          confirmLabel="Deactivate account"
          busy={busyId === confirming.id}
          error={confirmError}
          onConfirm={() => setStatus(confirming, false)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </section>
  );
}
