import React from 'react';
import { Zap } from 'lucide-react';
import type { ChatMode } from '../../types';

interface ModeToggleProps {
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  /** `sm` sits in the message box toolbar; `md` is the larger one on the welcome screen. */
  size?: 'sm' | 'md';
}

/** The RAG pipeline is the default; this button switches to the direct configured LLM path. */
export const nextChatMode = (mode: ChatMode): ChatMode => (mode === 'sprint' ? 'scholar' : 'sprint');

export const ModeToggle: React.FC<ModeToggleProps> = ({ mode, onModeChange, size = 'sm' }) => {
  const direct = mode === 'sprint';
  return (
    <button
      type="button"
      aria-pressed={direct}
      aria-label="High mode"
      title={direct ? 'High is on: direct responses from the configured LLM. Click to return to the RAG Pipeline.' : 'Turn on High for a direct response from the configured LLM (the RAG Pipeline is the default).'}
      onClick={() => onModeChange(nextChatMode(mode))}
      className={`inline-flex cursor-pointer items-center gap-1.5 rounded-full border font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-orange-500 ${
        size === 'md' ? 'h-9 px-5 text-sm' : 'h-7 px-3 text-[11px]'
      } ${
        direct
          ? 'border-[#ff6600] bg-orange-50 text-orange-700 shadow-sm dark:bg-orange-500/15 dark:text-orange-300'
          : 'border-slate-200 bg-white text-slate-600 hover:border-orange-400 hover:text-orange-600 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-300'
      }`}
    >
      High
      <Zap
        aria-hidden="true"
        strokeWidth={3}
        className={`text-emerald-500 dark:text-emerald-400 ${size === 'md' ? 'h-4 w-4' : 'h-3.5 w-3.5'}`}
      />
    </button>
  );
};
