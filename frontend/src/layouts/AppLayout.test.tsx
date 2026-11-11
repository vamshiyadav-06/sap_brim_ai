import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { User } from '../types';
import { userStorageKey } from '../services/userStorage';

const auth = vi.hoisted(() => ({ user: null as User | null }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: auth.user, logout: vi.fn() }) }));
vi.mock('../services/projectService', () => ({
  PROJECTS_CHANGED: 'test:projects-changed',
  projectService: { list: () => Promise.resolve([]) },
}));

const { AppLayout } = await import('./AppLayout');

afterEach(() => vi.unstubAllGlobals());

const renderLayout = (user: User) => {
  auth.user = user;
  const html = renderToStaticMarkup(
    <MemoryRouter initialEntries={['/app/settings']}>
      <AppLayout />
    </MemoryRouter>,
  );
  return html;
};

describe('AppLayout sidebar navigation', () => {
  it('starts Projects collapsed with an accessible toggle and keeps project creation outside the hidden list', () => {
    const html = renderLayout({ id: 'i', email: 'i@x.com', name: 'I' });
    expect(html).toMatch(/<button[^>]*aria-expanded="false" aria-controls="sidebar-project-list"[^>]*><span>Projects<\/span>/);
    expect(html).toContain('<div id="sidebar-project-list" hidden="">');
    expect(html.indexOf('aria-label="Create a project"')).toBeLessThan(html.indexOf('id="sidebar-project-list"'));
  });

  it('restores Projects separately from Chats and only for its owning profile', () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === userStorageKey('one', 'projects_expanded') ? 'true' : null,
    });
    const one = renderLayout({ id: 'one', email: 'one@x.com', name: 'One' });
    expect(one).toMatch(/aria-expanded="true" aria-controls="sidebar-project-list"/);
    expect(one).toContain('<div id="sidebar-project-list">');
    expect(one).toContain('<details class="sidebar-chats">');
    expect(renderLayout({ id: 'two', email: 'two@x.com', name: 'Two' })).toContain('<div id="sidebar-project-list" hidden="">');
  });

  it('starts Chats collapsed until opened, with Projects outside the dropdown', () => {
    const html = renderLayout({ id: 'i', email: 'i@x.com', name: 'I' });
    expect(html).toContain('<details class="sidebar-chats">');
    expect(html).toMatch(/<summary[^>]*aria-controls="sidebar-chat-list"[^>]*><span>Chats<\/span>/);
    expect(html).toContain('id="sidebar-chat-list"');
    expect(html.indexOf('</details>')).toBeLessThan(html.indexOf('aria-label="Create a project"'));
  });

  it('restores the signed-in account dropdown choice without applying it to other profiles', () => {
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => key === userStorageKey('one', 'chat_history_expanded') ? 'true' : null,
    });
    expect(renderLayout({ id: 'one', email: 'one@x.com', name: 'One' })).toContain('<details class="sidebar-chats" open="">');
    expect(renderLayout({ id: 'two', email: 'two@x.com', name: 'Two' })).toContain('<details class="sidebar-chats">');
  });

  it('keeps a saved collapsed choice when the workspace remounts', () => {
    vi.stubGlobal('localStorage', { getItem: () => 'false' });
    expect(renderLayout({ id: 'one', email: 'one@x.com', name: 'One' })).toContain('<details class="sidebar-chats">');
  });

  it('shows a medium sidebar logo in both themes and keeps the collapse control', () => {
    const html = renderLayout({ id: 'i', email: 'i@x.com', name: 'I' });
    const header = html.slice(html.indexOf('sidebar-top-header'), html.indexOf('sidebar-history-area'));
    expect(header.match(/<img class="h-10 w-auto object-contain/g)).toHaveLength(2);
    expect(header).toContain('aria-label="Collapse sidebar"');
  });

  it('shows no initial-letter avatar in the top bar (the profile card in the sidebar keeps it)', () => {
    const html = renderLayout({ id: 'e', email: 'e@x.com', name: 'Eve' });
    expect(html).not.toContain('top-avatar');
    expect(html).toContain('chatgpt-avatar');
    expect(html).toContain('New chat');
  });

  it.each<User>([
    { id: 'i', email: 'i@x.com', name: 'I', account_type: 'individual' },
    { id: 'a', email: 'a@x.com', name: 'A', is_admin: true },
  ])('omits the organisation shortcuts for $id while keeping Guide and Settings', (user) => {
    const html = renderLayout(user);
    expect(html).not.toMatch(/href="\/(?:app\/)?organisation/);
    expect(html).not.toContain('Knowledge Hub');
    expect(html).toContain('SAP + BRIM Guide');
    expect(html).toContain('href="/app/settings"');
  });

  const sidebarLinks = (html: string) => html.slice(html.indexOf('sidebar-bottom-section'));

  it('gives organisation members a way back to the Knowledge Hub, without the admin dashboard', () => {
    const html = sidebarLinks(renderLayout({ id: 'm', email: 'm@x.com', name: 'M', account_type: 'organisation', org_role: 'member' }));
    expect(html).toMatch(/<a[^>]*href="\/app\/organisation"[^>]*>[\s\S]*?Knowledge Hub<\/span><\/a>/);
    expect(html).not.toContain('href="/app/organisation/admin"');
    expect(html).not.toContain('Admin Dashboard');
    expect(html).toContain('href="/app/settings"');
  });

  it.each(['admin', 'owner'] as const)('gives an organisation %s the Knowledge Hub and their Admin Dashboard', (role) => {
    const html = sidebarLinks(renderLayout({ id: role, email: `${role}@x.com`, name: role, account_type: 'organisation', org_role: role }));
    expect(html).toMatch(/<a[^>]*href="\/app\/organisation"[^>]*>[\s\S]*?Knowledge Hub<\/span><\/a>/);
    expect(html).toMatch(/<a[^>]*href="\/app\/organisation\/admin"[^>]*>[\s\S]*?Admin Dashboard<\/span><\/a>/);
    expect(html).not.toContain('href="/admin"');
    expect(html.indexOf('Knowledge Hub')).toBeLessThan(html.indexOf('Admin Dashboard'));
  });

  it('keeps the Admin Dashboard shortcut for super admins', () => {
    const html = renderLayout({ id: 'a', email: 'a@x.com', name: 'A', is_admin: true });
    expect(html).toContain('href="/admin"');
    expect(html).toContain('Admin Dashboard');
  });
});
