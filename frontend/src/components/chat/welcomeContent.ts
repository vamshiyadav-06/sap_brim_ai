// Copy for the chat welcome screen: the two answer modes and starter prompts per mode.
import type React from 'react';
import {
  CircleHelp,
  FileText,
  GitCompareArrows,
  Search,
  Settings2,
  Workflow,
} from 'lucide-react';
import type { ChatMode } from '../../types';

export interface Suggestion {
  icon: React.ComponentType<{ size?: number }>;
  title: string;
  description: string;
  prompt: string;
}

export const MODES: { id: ChatMode; title: string; summary: string; detail: string }[] = [
  {
    id: 'scholar',
    title: 'RAG Pipeline',
    summary: 'Researched and verified',
    detail: 'Searches curated SAP documentation and checks each claim before answering. Takes longer.',
  },
  {
    id: 'sprint',
    title: 'Direct LLM',
    summary: 'Fast, direct answers',
    detail: 'Answers directly with the configured language model. Best for definitions and quick how-tos.',
  },
];

// Quick, conceptual questions suit Direct LLM; multi-step and configuration questions suit the RAG Pipeline.
export const SUGGESTIONS: Record<ChatMode, Suggestion[]> = {
  sprint: [
    {
      icon: Workflow,
      title: 'Explain a BRIM concept',
      description: 'Billable items, charge plans, provider contracts',
      prompt: 'What is a billable item in SAP Convergent Invoicing and how is it created?',
    },
    {
      icon: GitCompareArrows,
      title: 'Compare components',
      description: 'How the BRIM components fit together',
      prompt: 'What is the difference between SAP Convergent Charging and SAP Convergent Invoicing?',
    },
    {
      icon: Search,
      title: 'Find a transaction',
      description: 'Look up the right transaction code',
      prompt: 'Which SAP transaction is used to monitor billable items in Convergent Invoicing?',
    },
    {
      icon: Settings2,
      title: 'FI-CA basics',
      description: 'Contract accounts and postings',
      prompt: 'What is a contract account in SAP FI-CA and what is it used for?',
    },
  ],
  scholar: [
    {
      icon: Workflow,
      title: 'End-to-end process',
      description: 'From SOM order to FI-CA posting',
      prompt: 'Explain the end-to-end SAP BRIM flow from a subscription order in SOM to the posting in FI-CA.',
    },
    {
      icon: Settings2,
      title: 'Configuration path',
      description: 'Where a setting is maintained in Customizing',
      prompt: 'How are document types and posting areas configured in SAP FI-CA?',
    },
    {
      icon: CircleHelp,
      title: 'Troubleshoot an issue',
      description: 'Start from an error or a billing symptom',
      prompt: 'My billing run completed in Convergent Invoicing but no invoicing document was created. What should I check?',
    },
    {
      icon: FileText,
      title: 'Reconcile postings',
      description: 'Billable items, invoices and FI-CA',
      prompt: 'How do I reconcile billable items, invoicing documents and FI-CA postings after a failed invoicing run?',
    },
  ],
};
