import { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';

/** Show/hide state for one password field: each field gets its own eye. */
export function usePasswordReveal() {
  const [visible, setVisible] = useState(false);
  return { visible, type: visible ? 'text' : 'password', toggle: () => setVisible((v) => !v) } as const;
}

/** The eye button that sits inside a password field. `className` positions it for the surrounding form style. */
export function RevealButton({
  visible,
  onToggle,
  className = 'auth-reveal',
  label = 'password',
}: {
  visible: boolean;
  onToggle: () => void;
  className?: string;
  label?: string;
}) {
  return (
    <button
      type="button"
      className={className}
      onClick={onToggle}
      aria-label={`${visible ? 'Hide' : 'Show'} ${label}`}
      aria-pressed={visible}
    >
      {visible ? <EyeOff size={16} aria-hidden="true" /> : <Eye size={16} aria-hidden="true" />}
    </button>
  );
}
