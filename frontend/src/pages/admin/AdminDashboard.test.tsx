import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({ user: { id: 'admin-1', email: 'admin@clyptus.example', name: 'Admin', is_admin: true }, logout: vi.fn() }),
}));
// The document manager has its own data loading; it is not part of this test.
vi.mock('../DocumentManagementPage', () => ({ DocumentManagementPage: () => <div>documents tab</div> }));

const { AdminDashboard } = await import('./AdminDashboard');

const html = renderToStaticMarkup(
  <MemoryRouter initialEntries={['/admin']}>
    <AdminDashboard />
  </MemoryRouter>,
);

const tabLabels = [...html.matchAll(/<button type="button" aria-pressed="(true|false)"[^>]*>.*?<span>([^<]+)<\/span><\/button>/g)].map(
  ([, pressed, label]) => ({ label, pressed: pressed === 'true' }),
);

const groupToggles = [
  ...html.matchAll(/<button type="button" aria-expanded="(true|false)" aria-controls="([^"]+)"[^>]*>.*?<span>([^<]+)<\/span>/g),
].map(([, expanded, controls, label]) => ({ label, controls, expanded: expanded === 'true' }));

describe('AdminDashboard account management tabs', () => {
  it('uses light page, header and text colours with explicit dark variants', () => {
    expect(html).toContain('min-h-screen bg-white dark:bg-[#07090e] text-slate-900 dark:text-zinc-100');
    expect(html).toContain('bg-white/95 dark:bg-[#0c0e15]/95');
  });

  it('groups the sections under two toggles, Accounts first', () => {
    expect(groupToggles).toEqual([
      { label: 'Accounts', controls: 'admin-accounts-options', expanded: true },
      { label: 'Knowledge base', controls: 'admin-knowledge-options', expanded: false },
    ]);
  });

  it('shows only the open Accounts toggle sub-options: Organisations and Individual users', () => {
    expect(tabLabels.map((t) => t.label)).toEqual(['Organisations', 'Individual users']);
    expect(html).toContain('id="admin-accounts-options"');
    expect(html).not.toContain('id="admin-knowledge-options"');
    for (const hidden of ['Document Manager', 'Internal Sources', 'Retrieval &amp; Chunk Sandbox', 'System &amp; Security Policy']) {
      expect(html).not.toContain(hidden);
    }
  });

  it('opens on Organisation Management only, not the individual user list', () => {
    expect(tabLabels.filter((t) => t.pressed).map((t) => t.label)).toEqual(['Organisations']);
    expect(html).toContain('Organisation Management');
    expect(html).toContain('Create organisation');
    expect(html).not.toContain('Individual User Management');
    expect(html).not.toContain('documents tab');
  });

  it('shows the new-user sign-in control in the top navigation without opening System', () => {
    expect(html).toContain('aria-label="Enable new user sign-in"');
    expect(html).toContain('Loading sign-up…');
  });

  it('offers the status filter for organisations', () => {
    for (const status of ['All', 'Requested', 'Pending activation', 'Active', 'Suspended', 'Deactivated', 'Rejected']) {
      expect(html).toMatch(new RegExp(`aria-pressed="(true|false)">${status}</button>`));
    }
  });
});
