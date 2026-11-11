import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import type { User } from '../../types';

vi.mock('../../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'u', email: 'u@x.com', name: 'U', must_change_password: true } as User,
    token: null,
    logout: vi.fn(),
    register: vi.fn(),
    adminLogin: vi.fn(),
    applySession: vi.fn(),
  }),
}));

const { ActivationAccountForm } = await import('../organisation/ActivationAccountForm');
const { OrgChangePasswordPage } = await import('../../pages/organisation/OrgChangePasswordPage');
const { RegisterPage } = await import('../../pages/RegisterPage');
const { AdminLoginPage } = await import('../../pages/admin/AdminLoginPage');
const { IndividualUsersAdminPanel } = await import('../admin/IndividualUsersAdminPanel');

const eyes = (html: string) => html.match(/<button[^>]*aria-label="Show [^"]*password"[^>]*aria-pressed="false"/g) ?? [];
const passwordInputs = (html: string) => html.match(/<input[^>]*type="password"[^>]*>/g) ?? [];

describe('every password field has its own show/hide eye', () => {
  it('organisation activation: password and confirmation', () => {
    const html = renderToStaticMarkup(
      <ActivationAccountForm
        details={{ organisation_name: 'Acme', admin_email: 'a@x.com', admin_name: 'A' } as never}
        submitting={false}
        error={null}
        onSubmit={vi.fn()}
        onBack={vi.fn()}
      />,
    );
    expect(passwordInputs(html)).toHaveLength(2);
    expect(eyes(html)).toHaveLength(2);
  });

  it('organisation change of the temporary password: current, new and confirmation', () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <OrgChangePasswordPage />
      </MemoryRouter>,
    );
    expect(passwordInputs(html)).toHaveLength(3);
    expect(eyes(html)).toHaveLength(3);
  });

  it('individual sign-up', () => {
    const html = renderToStaticMarkup(<RegisterPage onSwitchToLogin={vi.fn()} />);
    expect(passwordInputs(html)).toHaveLength(1);
    expect(eyes(html)).toHaveLength(1);
  });

  it('super admin sign-in', () => {
    vi.stubGlobal('window', { location: { search: '' } });
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <AdminLoginPage />
      </MemoryRouter>,
    );
    vi.unstubAllGlobals();
    expect(passwordInputs(html)).toHaveLength(1);
    expect(eyes(html)).toHaveLength(1);
  });
});

describe('IndividualUsersAdminPanel', () => {
  it('renders without a visible password field until the create form opens', () => {
    expect(passwordInputs(renderToStaticMarkup(<IndividualUsersAdminPanel />))).toHaveLength(0);
  });
});
