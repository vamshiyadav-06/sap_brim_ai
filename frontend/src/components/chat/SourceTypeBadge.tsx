import React from 'react';
import { Database, Globe, Layers, AlertCircle, Sparkles, GitBranch, FileText, Zap } from 'lucide-react';

interface SourceTypeBadgeProps {
  sourceType?: 'knowledge_base' | 'github_repo' | 'web' | 'combined' | 'abstention' | 'insufficient' | 'refusal' | 'error' | 'interrupted' | 'security_policy' | 'conversational' | 'attached_document' | 'unavailable' | 'sprint';
}

export const SourceTypeBadge: React.FC<SourceTypeBadgeProps> = ({ sourceType }) => {
  if (!sourceType) return null;

  if (sourceType === 'attached_document') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-blue-500/15 text-blue-600 dark:text-blue-300 border border-blue-500/30">
        <FileText className="w-3 h-3 text-blue-500 dark:text-blue-400" />
        <span>Attached Document Analysis</span>
      </span>
    );
  }

  if (sourceType === 'conversational') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
        <Sparkles className="w-3 h-3 text-emerald-400" />
        <span>SAP BRIM Assistant</span>
      </span>
    );
  }

  if (sourceType === 'interrupted') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/15 text-amber-300 border border-amber-500/30">
        <AlertCircle className="w-3 h-3 text-amber-400" />
        <span>Generation Paused</span>
      </span>
    );
  }

  if (sourceType === 'unavailable') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/15 text-amber-300 border border-amber-500/30">
        <AlertCircle className="w-3 h-3 text-amber-400" />
        <span>Response unavailable</span>
      </span>
    );
  }

  if (sourceType === 'sprint') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-orange-500/15 text-orange-300 border border-orange-500/30">
        <Zap className="w-3 h-3 text-orange-400" />
        <span>Direct LLM Answer</span>
      </span>
    );
  }

  if (sourceType === 'knowledge_base') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-orange-500/15 text-orange-300 border border-orange-500/30">
        <Database className="w-3 h-3 text-orange-400" />
        <span>SAP Knowledge Base</span>
      </span>
    );
  }

  if (sourceType === 'web') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
        <Globe className="w-3 h-3 text-cyan-400" />
        <span>SAP Documentation (help.sap.com)</span>
      </span>
    );
  }

  if (sourceType === 'github_repo') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-slate-500/15 text-slate-700 dark:text-slate-300 border border-slate-500/30">
        <GitBranch className="w-3 h-3" />
        <span>GitHub SAP Repository</span>
      </span>
    );
  }

  if (sourceType === 'combined') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-purple-500/15 text-purple-300 border border-purple-500/30">
        <Layers className="w-3 h-3 text-purple-400" />
        <span>Multi-Source Grounded</span>
      </span>
    );
  }

  if (sourceType === 'refusal') {
    return (
      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-rose-500/15 text-rose-300 border border-rose-500/30">
        <AlertCircle className="w-3 h-3 text-rose-400" />
        <span>SAP Scope Guardrail</span>
      </span>
    );
  }

  return null;
};
