import React, { useState, useEffect } from 'react';
import {
  ShieldCheck,
  Database,
  FileText,
  Search,
  RefreshCw,
  Layers,
  Cpu,
  CheckCircle2,
  AlertTriangle,
  Play,
  LogOut,
  MessageSquare,
  Server,
  Eye,
  Sliders,
  Check,
  Building2,
  UserRound,
  Users,
  BookOpen,
  ChevronDown,
} from 'lucide-react';

import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../../context/AuthContext';
import { DocumentManagementPage } from '../DocumentManagementPage';
import { OrganisationsAdminPanel } from '../../components/admin/OrganisationsAdminPanel';
import { IndividualUsersAdminPanel } from '../../components/admin/IndividualUsersAdminPanel';
import { AuthenticationControls } from '../../components/admin/AuthenticationControls';
import { NewUserSignInToggle } from '../../components/admin/NewUserSignInToggle';
import {
  apiGetRAGInspectionDocuments,
  apiGetSystemConfig,
  apiTestSimilaritySearch,
  apiTriggerIngest,
} from '../../services/api';
import type { SystemConfig } from '../../services/api';
import { PageMeta } from '../../components/ui/PageMeta';
import { useRefreshSpinner } from '../../components/ui/useRefreshSpinner';
import iconOnly from '../../assets/clyptusap-icon.png';

interface InspectionDoc {
  document_id: string | null;
  title: string;
  filename: string;
  file_size_mb: number;
  file_path: string;
  page_count: number;
  chunk_count: number;
  embedded_count: number;
  embedding_coverage: number;
  is_indexed: boolean;
  status: string;
  file_hash: string | null;
}

export const AdminDashboard: React.FC = () => {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  // Account management (organisations, individual users) comes first; knowledge-base tools follow.
  const [activeTab, setActiveTab] = useState<
    'organisations' | 'individuals' | 'documents' | 'sources' | 'sandbox' | 'system'
  >('organisations');

  // Sub-options are grouped under two toggles; only the open group's options are shown.
  // Opening a group does not change the active tab until one of its options is chosen.
  const [openGroup, setOpenGroup] = useState<'accounts' | 'knowledge' | null>('accounts');
  const accountsTabActive = activeTab === 'organisations' || activeTab === 'individuals';
  const toggleGroup = (group: 'accounts' | 'knowledge') =>
    setOpenGroup((current) => (current === group ? null : group));

  // Sources tab state
  const [sourcesLoading, setSourcesLoading] = useState(false);
  const sourcesSpinner = useRefreshSpinner();
  const [sourcesData, setSourcesData] = useState<{
    summary: any;
    documents: InspectionDoc[];
  } | null>(null);
  const [sourcesSearch, setSourcesSearch] = useState('');
  const [ingestTriggering, setIngestTriggering] = useState(false);
  const [ingestNotice, setIngestNotice] = useState<string | null>(null);

  // Sandbox tab state
  const [testQuery, setTestQuery] = useState('How does Convergent Invoicing aggregate billable items in SAP BRIM?');
  const [testTopK, setTestTopK] = useState(6);
  const [includeWeb, setIncludeWeb] = useState(false);
  const [sandboxRunning, setSandboxRunning] = useState(false);
  const [sandboxResult, setSandboxResult] = useState<any | null>(null);
  const [sandboxError, setSandboxError] = useState<string | null>(null);

  const fetchSources = async () => {
    setSourcesLoading(true);
    try {
      const data = await apiGetRAGInspectionDocuments();
      setSourcesData(data);
    } catch (err: any) {
      console.error('Failed to load internal sources:', err);
    } finally {
      setSourcesLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === 'sources') {
      fetchSources();
    }
  }, [activeTab]);

  // System tab: live settings from the backend, never hard-coded copies.
  const [systemConfig, setSystemConfig] = useState<SystemConfig | null>(null);
  const [systemError, setSystemError] = useState<string | null>(null);
  useEffect(() => {
    if (activeTab !== 'system') return;
    setSystemError(null);
    apiGetSystemConfig()
      .then(setSystemConfig)
      .catch((err: Error) => setSystemError(err.message));
  }, [activeTab]);

  const handleTriggerIngest = async () => {
    setIngestTriggering(true);
    setIngestNotice(null);
    try {
      const result = await apiTriggerIngest();
      setIngestNotice(`Ingestion completed: ${result.message || 'Knowledge base updated.'}`);
      fetchSources();
    } catch (err: any) {
      setIngestNotice(`Ingestion error: ${err.message || 'Operation failed'}`);
    } finally {
      setIngestTriggering(false);
    }
  };

  const handleRunSandbox = async (queryToRun?: string) => {
    const q = queryToRun || testQuery;
    if (!q.trim()) return;
    setSandboxRunning(true);
    setSandboxError(null);
    try {
      const data = await apiTestSimilaritySearch(q.trim(), testTopK, includeWeb);
      setSandboxResult(data);
    } catch (err: any) {
      setSandboxError(err.message || 'Failed to execute similarity inspection');
    } finally {
      setSandboxRunning(false);
    }
  };

  const samplePrompts = [
    'How does Convergent Invoicing aggregate billable items in SAP BRIM?',
    'What is a provider contract in Subscription Order Management (SOM)?',
    'How do pricing trees and allowances operate in SAP Convergent Charging?',
    'What are the integration touchpoints between FICA and General Ledger in BRIM?',
  ];

  const filteredSources = (sourcesData?.documents || []).filter((doc) => {
    if (!sourcesSearch.trim()) return true;
    const query = sourcesSearch.toLowerCase();
    return (
      doc.filename.toLowerCase().includes(query) ||
      doc.title.toLowerCase().includes(query) ||
      (doc.file_path && doc.file_path.toLowerCase().includes(query))
    );
  });

  type AdminTab = 'organisations' | 'individuals' | 'documents' | 'sources' | 'sandbox' | 'system';
  const accountTabs: { id: AdminTab; label: string; icon: typeof Building2 }[] = [
    { id: 'organisations', label: 'Organisations', icon: Building2 },
    { id: 'individuals', label: 'Individual users', icon: UserRound },
  ];
  const knowledgeTabs: { id: AdminTab; label: string; icon: typeof Building2 }[] = [
    { id: 'documents', label: 'Document Manager & Ingestion', icon: Database },
    { id: 'sources', label: 'Internal Sources & Repository', icon: Layers },
    { id: 'sandbox', label: 'Retrieval & Chunk Sandbox', icon: Sliders },
    { id: 'system', label: 'System & Security Policy', icon: Cpu },
  ];
  const focusRing = 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60';
  const groupButtonClass = (open: boolean) =>
    `inline-flex items-center gap-2 h-9 px-3.5 rounded-lg text-xs font-bold uppercase tracking-wider transition whitespace-nowrap cursor-pointer border ${focusRing} ${
      open
        ? 'bg-slate-100 dark:bg-zinc-800 text-slate-900 dark:text-white border-amber-500/60'
        : 'bg-transparent text-slate-600 dark:text-zinc-400 hover:text-slate-800 dark:hover:text-zinc-200 hover:bg-slate-100/60 dark:hover:bg-zinc-800/60 border-slate-200 dark:border-zinc-700'
    }`;
  const tabButtonClass = (active: boolean) =>
    `inline-flex items-center gap-2 h-8 px-3 rounded-lg text-xs font-semibold transition whitespace-nowrap cursor-pointer ${focusRing} ${
      active
        ? 'bg-amber-500 text-slate-950 shadow-sm'
        : 'text-slate-600 dark:text-zinc-400 hover:text-slate-900 dark:hover:text-white hover:bg-white dark:hover:bg-zinc-800'
    }`;

  return (
    <>
      <PageMeta
        title="Admin Control Center"
        description="Enterprise Knowledge Administrator Dashboard for Clyptus.AI SAP Knowledge Assistant."
      />

      <div className="min-h-screen bg-white dark:bg-[#07090e] text-slate-900 dark:text-zinc-100 flex flex-col font-sans">
        {/* Top Navigation Bar */}
        <header className="sticky top-0 z-30 bg-white/95 dark:bg-[#0c0e15]/95 backdrop-blur-md border-b border-slate-200 dark:border-zinc-800">
          {/* Row 1: brand (left) and account actions (right), all controls share one height */}
          <div className="max-w-7xl mx-auto px-4 md:px-8 py-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-3">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-10 h-10 shrink-0 rounded-xl bg-gradient-to-tr from-amber-600 via-orange-500 to-amber-400 p-0.5 shadow-sm shadow-amber-500/20">
                <div className="w-full h-full bg-white dark:bg-[#0d0f17] rounded-[10px] flex items-center justify-center p-1.5">
                  <img src={iconOnly} alt="Clyptus.AI" className="w-full h-full object-contain" />
                </div>
              </div>

              <div className="min-w-0 leading-tight">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-sm tracking-tight text-slate-900 dark:text-white whitespace-nowrap">
                    Clyptus.AI <span className="text-amber-700 dark:text-amber-400">Enterprise</span>
                  </span>
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-mono font-bold bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/30 inline-flex items-center gap-1 whitespace-nowrap">
                    <ShieldCheck size={11} className="text-amber-700 dark:text-amber-400" />
                    SUPER ADMIN
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] font-mono text-slate-600 dark:text-zinc-400 flex items-center gap-1.5 min-w-0">
                  <span className="w-1.5 h-1.5 shrink-0 rounded-full bg-emerald-500 animate-pulse" aria-hidden="true" />
                  <span className="truncate">{user?.email}</span>
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center justify-end gap-2">
              <NewUserSignInToggle />
              <span className="hidden md:block w-px h-6 bg-slate-200 dark:bg-zinc-700" aria-hidden="true" />
              <Link
                to="/app"
                className="inline-flex items-center justify-center gap-2 h-9 px-3.5 rounded-lg bg-white dark:bg-zinc-900 hover:bg-slate-50 dark:hover:bg-zinc-800 text-slate-800 dark:text-zinc-200 text-xs font-semibold transition border border-slate-200 dark:border-zinc-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60"
                title="Switch to user chat interface"
                aria-label="User Chat Workspace"
              >
                <MessageSquare size={14} className="text-amber-700 dark:text-amber-400" />
                <span className="hidden sm:inline">User Chat Workspace</span>
              </Link>

              <button
                type="button"
                onClick={() => {
                  logout();
                  navigate('/admin/login', { replace: true });
                }}
                className="inline-flex items-center justify-center gap-2 h-9 px-3.5 rounded-lg bg-rose-500/10 hover:bg-rose-500/20 text-rose-700 dark:text-rose-400 text-xs font-semibold transition border border-rose-500/30 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/50"
                title="Sign out of administrator session"
                aria-label="Sign Out"
              >
                <LogOut size={14} />
                <span className="hidden sm:inline">Sign Out</span>
              </button>
            </div>
          </div>

          {/* Row 2: section groups and the open group's options sit on one aligned line */}
          <div className="border-t border-slate-200/70 dark:border-zinc-800/70">
            <div className="max-w-7xl mx-auto px-4 md:px-8 py-2.5 flex flex-wrap items-center gap-x-3 gap-y-2">
              <div className="flex items-center gap-2" role="group" aria-label="Admin sections">
                <button
                  type="button"
                  onClick={() => toggleGroup('accounts')}
                  aria-expanded={openGroup === 'accounts'}
                  aria-controls="admin-accounts-options"
                  className={groupButtonClass(openGroup === 'accounts')}
                >
                  <Users size={14} className={accountsTabActive ? 'text-amber-700 dark:text-amber-400' : undefined} />
                  <span>Accounts</span>
                  <ChevronDown
                    size={14}
                    aria-hidden="true"
                    className={`transition-transform ${openGroup === 'accounts' ? 'rotate-180' : ''}`}
                  />
                </button>

                <button
                  type="button"
                  onClick={() => toggleGroup('knowledge')}
                  aria-expanded={openGroup === 'knowledge'}
                  aria-controls="admin-knowledge-options"
                  className={groupButtonClass(openGroup === 'knowledge')}
                >
                  <BookOpen size={14} className={accountsTabActive ? undefined : 'text-amber-700 dark:text-amber-400'} />
                  <span>Knowledge base</span>
                  <ChevronDown
                    size={14}
                    aria-hidden="true"
                    className={`transition-transform ${openGroup === 'knowledge' ? 'rotate-180' : ''}`}
                  />
                </button>
              </div>

              {openGroup && <span className="hidden sm:block w-px h-6 bg-slate-200 dark:bg-zinc-700" aria-hidden="true" />}

              {openGroup === 'accounts' && (
                <div
                  id="admin-accounts-options"
                  className="flex flex-wrap items-center gap-1 p-1 rounded-xl bg-slate-100 dark:bg-zinc-900 border border-slate-200/70 dark:border-zinc-800"
                >
                  {accountTabs.map((tab) => (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => setActiveTab(tab.id)}
                      aria-pressed={activeTab === tab.id}
                      className={tabButtonClass(activeTab === tab.id)}
                    >
                      <tab.icon size={14} />
                      <span>{tab.label}</span>
                    </button>
                  ))}
                </div>
              )}

              {openGroup === 'knowledge' && (
                <div
                  id="admin-knowledge-options"
                  className="flex flex-wrap items-center gap-1 p-1 rounded-xl bg-slate-100 dark:bg-zinc-900 border border-slate-200/70 dark:border-zinc-800"
                >
                  {knowledgeTabs.map((tab) => (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => setActiveTab(tab.id)}
                      aria-pressed={activeTab === tab.id}
                      className={tabButtonClass(activeTab === tab.id)}
                    >
                      <tab.icon size={14} />
                      <span>{tab.label}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        </header>

        {/* Account management: organisations and individual users are separate sections. */}
        {activeTab === 'organisations' && (
          <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-8">
            <OrganisationsAdminPanel />
          </main>
        )}

        {activeTab === 'individuals' && (
          <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-8">
            <IndividualUsersAdminPanel currentUserId={user?.id} />
          </main>
        )}

        {/* Tab 1: Documents Management */}
        {activeTab === 'documents' && (
          <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-8">
            <div className="mb-6 p-4 rounded-2xl bg-amber-500/10 border border-amber-500/30 flex items-center gap-4">
              <div className="flex items-center gap-3">
                <div className="p-2.5 rounded-xl bg-amber-500/20 text-amber-700 dark:text-amber-400">
                  <ShieldCheck size={20} />
                </div>
                <div>
                  <h2 className="text-sm font-bold text-slate-900 dark:text-white">Administrator Document Control</h2>
                  <p className="text-xs text-slate-700 dark:text-zinc-300">
                    Uploaded documents and chunk embeddings are only accessible and managed within this dashboard. Regular users cannot access this module.
                  </p>
                </div>
              </div>
            </div>

            <DocumentManagementPage />
          </main>
        )}

        {/* Tab 2: Internal Sources & Knowledge Repository Inspector */}
        {activeTab === 'sources' && (
          <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-8 space-y-6">
            <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-slate-200 dark:border-zinc-800">
              <div>
                <div className="flex items-center gap-2.5 mb-1">
                  <span className="p-2 rounded-xl bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/20">
                    <Layers size={22} strokeWidth={2.2} />
                  </span>
                  <h1 className="text-2xl font-black tracking-tight text-slate-900 dark:text-white">
                    Internal Knowledge Sources & Repository
                  </h1>
                </div>
                <p className="text-xs md:text-sm text-slate-600 dark:text-zinc-400">
                  Comprehensive audit of authoritative internal documents, chunk distribution, and vector coverage strictly reserved for administrators.
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => { sourcesSpinner.start(); void fetchSources().finally(sourcesSpinner.stop); }}
                  disabled={sourcesLoading || sourcesSpinner.spinning}
                  className="inline-flex items-center justify-center gap-2 h-9 px-3.5 text-xs font-semibold rounded-lg border border-slate-200 dark:border-zinc-700 hover:bg-slate-100 dark:hover:bg-zinc-800 text-slate-700 dark:text-zinc-300 transition cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
                >
                  <RefreshCw size={14} className={sourcesLoading || sourcesSpinner.spinning ? 'animate-spin' : ''} />
                  Refresh
                </button>

                <button
                  type="button"
                  onClick={handleTriggerIngest}
                  disabled={ingestTriggering}
                  className="inline-flex items-center justify-center gap-2 h-9 px-4 text-xs font-bold rounded-lg bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-400 hover:to-orange-400 text-slate-950 shadow-sm shadow-amber-500/20 transition cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
                >
                  <Play size={14} className="fill-current" />
                  {ingestTriggering ? 'Running Ingestion...' : 'Trigger Ingestion Pipeline'}
                </button>
              </div>
            </div>

            {ingestNotice && (
              <div className="p-3.5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs text-amber-700 dark:text-amber-300 flex items-center justify-between">
                <span>{ingestNotice}</span>
                <button type="button" aria-label="Dismiss notice" onClick={() => setIngestNotice(null)} className="ml-3 shrink-0 w-6 h-6 inline-flex items-center justify-center rounded-md text-amber-700 dark:text-amber-400 hover:bg-amber-500/20 hover:text-slate-900 dark:hover:text-white cursor-pointer">✕</button>
              </div>
            )}

            {/* Metrics Overview Cards */}
            {sourcesData?.summary && (
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200/80 dark:border-zinc-800/80 shadow-sm">
                  <span className="text-[11px] font-mono text-slate-600 dark:text-zinc-400 block mb-1">TOTAL DISCOVERED</span>
                  <strong className="text-2xl font-bold text-slate-900 dark:text-white">
                    {sourcesData.summary.total_files_discovered}
                  </strong>
                  <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-0.5">Files in knowledge_base</span>
                </div>

                <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200/80 dark:border-zinc-800/80 shadow-sm">
                  <span className="text-[11px] font-mono text-emerald-700 dark:text-emerald-400 block mb-1">INDEXED SOURCES</span>
                  <strong className="text-2xl font-bold text-emerald-700 dark:text-emerald-400">
                    {sourcesData.summary.source_files_indexed}
                  </strong>
                  <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-0.5">Fully chunked & vectorized</span>
                </div>

                <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200/80 dark:border-zinc-800/80 shadow-sm">
                  <span className="text-[11px] font-mono text-amber-700 dark:text-amber-400 block mb-1">TOTAL CHUNKS</span>
                  <strong className="text-2xl font-bold text-amber-700 dark:text-amber-400">
                    {sourcesData.summary.total_chunks}
                  </strong>
                  <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-0.5">Semantic document chunks</span>
                </div>

                <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200/80 dark:border-zinc-800/80 shadow-sm">
                  <span className="text-[11px] font-mono text-cyan-700 dark:text-cyan-400 block mb-1">VECTOR COVERAGE</span>
                  <strong className="text-2xl font-bold text-cyan-700 dark:text-cyan-400">
                    {Math.round((sourcesData.summary.overall_embedding_coverage || 0) * 100)}%
                  </strong>
                  <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-0.5">{sourcesData.summary.embedding_model}</span>
                </div>
              </div>
            )}

            {/* Search Input */}
            <div className="flex items-center gap-3">
              <div className="relative flex-1">
                <Search size={15} className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-500 dark:text-zinc-500" />
                <input
                  type="text"
                  value={sourcesSearch}
                  onChange={(e) => setSourcesSearch(e.target.value)}
                  placeholder="Search internal knowledge files by filename, title, or path..."
                  className="w-full h-10 bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800 focus:border-amber-500 rounded-lg pl-10 pr-4 text-xs text-slate-900 dark:text-white placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-amber-500"
                />
              </div>
            </div>

            {/* Sources Table */}
            <div className="overflow-x-auto rounded-2xl border border-slate-200 dark:border-zinc-800 bg-white dark:bg-[#0d1018]">
              <table className="w-full text-left text-xs divide-y divide-slate-200 dark:divide-zinc-800">
                <thead className="bg-white dark:bg-[#121520] text-slate-600 dark:text-zinc-400 font-mono text-[11px]">
                  <tr>
                    <th className="px-4 py-3 font-semibold">Document Title & Filename</th>
                    <th className="px-4 py-3 font-semibold">Size</th>
                    <th className="px-4 py-3 font-semibold">Chunks</th>
                    <th className="px-4 py-3 font-semibold">Embeddings</th>
                    <th className="px-4 py-3 font-semibold">Coverage</th>
                    <th className="px-4 py-3 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200/60 dark:divide-zinc-800/60">
                  {sourcesLoading ? (
                    <tr>
                      <td colSpan={6} className="px-4 py-10 text-center text-slate-500 dark:text-zinc-500">
                        <RefreshCw size={20} className="animate-spin mx-auto mb-2 text-amber-700 dark:text-amber-500" />
                        Scanning knowledge base files...
                      </td>
                    </tr>
                  ) : filteredSources.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="px-4 py-8 text-center text-slate-500 dark:text-zinc-500">
                        No internal knowledge source files match your filter.
                      </td>
                    </tr>
                  ) : (
                    filteredSources.map((doc, idx) => (
                      <tr key={idx} className="hover:bg-slate-100/40 dark:hover:bg-zinc-800/40 transition">
                        <td className="px-4 py-3 font-medium text-slate-900 dark:text-white max-w-sm">
                          <div className="flex items-center gap-2">
                            <FileText size={15} className="text-amber-700 dark:text-amber-400 shrink-0" />
                            <div className="min-w-0">
                              <span className="font-semibold block truncate">{doc.title}</span>
                              <span className="text-[11px] font-mono text-slate-600 dark:text-zinc-400 block truncate">{doc.filename}</span>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3 font-mono text-slate-700 dark:text-zinc-300 whitespace-nowrap">
                          {doc.file_size_mb} MB
                        </td>
                        <td className="px-4 py-3 font-mono text-slate-700 dark:text-zinc-300">
                          {doc.chunk_count}
                        </td>
                        <td className="px-4 py-3 font-mono text-slate-700 dark:text-zinc-300">
                          {doc.embedded_count}
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap">
                          <div className="flex items-center gap-2">
                            <div className="w-16 h-1.5 bg-slate-100 dark:bg-zinc-800 rounded-full overflow-hidden">
                              <div
                                className="h-full bg-emerald-500 rounded-full"
                                style={{ width: `${Math.round(doc.embedding_coverage * 100)}%` }}
                              />
                            </div>
                            <span className="font-mono text-[11px] text-slate-600 dark:text-zinc-400">
                              {Math.round(doc.embedding_coverage * 100)}%
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap">
                          {doc.is_indexed ? (
                            <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-semibold bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 border border-emerald-500/30">
                              <CheckCircle2 size={10} /> Indexed
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[10px] font-semibold bg-slate-100/40 dark:bg-zinc-700/40 text-slate-600 dark:text-zinc-400 border border-slate-200 dark:border-zinc-700">
                              Pending Ingestion
                            </span>
                          )}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </main>
        )}

        {/* Tab 3: Retrieval & Chunk Sandbox */}
        {activeTab === 'sandbox' && (
          <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-8 space-y-6">
            <div className="pb-4 border-b border-slate-200 dark:border-zinc-800">
              <div className="flex items-center gap-2.5 mb-1">
                <span className="p-2 rounded-xl bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/20">
                  <Sliders size={22} strokeWidth={2.2} />
                </span>
                <h1 className="text-2xl font-black tracking-tight text-slate-900 dark:text-white">
                  Retrieval & Chunk Score Inspector Sandbox
                </h1>
              </div>
              <p className="text-xs md:text-sm text-slate-600 dark:text-zinc-400">
                Run ad-hoc queries through the dual retrieval engine to inspect internal chunks, cosine similarity, rerank weights, and evidence winner. These metrics are strictly invisible to regular users.
              </p>
            </div>

            {/* Sandbox input card */}
            <div className="p-6 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800 space-y-4">
              <div>
                <label className="block text-xs font-semibold text-slate-700 dark:text-zinc-300 mb-1.5">
                  Test Query / SAP Technical Prompt
                </label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={testQuery}
                    onChange={(e) => setTestQuery(e.target.value)}
                    placeholder="Enter SAP BRIM question to inspect retrieved internal chunks..."
                    className="flex-1 min-w-0 h-10 bg-white dark:bg-[#121520] border border-slate-200/80 dark:border-zinc-700/80 focus:border-amber-500 rounded-lg px-4 text-xs text-slate-900 dark:text-white placeholder-zinc-500 focus:outline-none focus:ring-1 focus:ring-amber-500 font-sans"
                  />
                  <button
                    type="button"
                    onClick={() => handleRunSandbox()}
                    disabled={sandboxRunning || !testQuery.trim()}
                    className="h-10 px-5 shrink-0 rounded-lg bg-gradient-to-r from-amber-500 to-orange-500 hover:from-amber-400 hover:to-orange-400 text-slate-950 font-bold text-xs shadow-sm shadow-amber-500/20 transition active:scale-95 disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer inline-flex items-center justify-center gap-2"
                  >
                    {sandboxRunning ? (
                      <>
                        <RefreshCw size={14} className="animate-spin" />
                        <span>Evaluating...</span>
                      </>
                    ) : (
                      <>
                        <Eye size={14} />
                        <span>Inspect Chunks</span>
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Quick Prompt Pills */}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <span className="text-[11px] font-mono text-slate-500 dark:text-zinc-500">Quick tests:</span>
                {samplePrompts.map((p, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => {
                      setTestQuery(p);
                      handleRunSandbox(p);
                    }}
                    className="text-[11px] px-2.5 py-1 rounded-lg bg-slate-100/60 dark:bg-zinc-800/60 hover:bg-slate-100 dark:hover:bg-zinc-700 text-slate-700 dark:text-zinc-300 hover:text-slate-900 dark:hover:text-white transition cursor-pointer border border-slate-200/40 dark:border-zinc-700/40"
                  >
                    {p.length > 40 ? p.substring(0, 40) + '...' : p}
                  </button>
                ))}
              </div>

              {/* Controls */}
              <div className="flex flex-wrap items-center gap-6 pt-2 border-t border-slate-200/70 dark:border-zinc-800/70 text-xs">
                <div className="flex items-center gap-2">
                  <span className="text-slate-600 dark:text-zinc-400 font-medium">Top Chunks:</span>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    value={testTopK}
                    onChange={(e) => setTestTopK(parseInt(e.target.value) || 5)}
                    className="w-16 bg-white dark:bg-[#121520] border border-slate-200 dark:border-zinc-700 rounded-lg px-2.5 py-1 text-center text-xs text-slate-900 dark:text-white"
                  />
                </div>

                <label className="flex items-center gap-2 cursor-pointer select-none text-slate-700 dark:text-zinc-300">
                  <input
                    type="checkbox"
                    checked={includeWeb}
                    onChange={(e) => setIncludeWeb(e.target.checked)}
                    className="rounded bg-slate-100 dark:bg-zinc-800 border-slate-200 dark:border-zinc-700 text-amber-700 dark:text-amber-500 focus:ring-amber-500"
                  />
                  <span>Simulate Web Fallback Comparison</span>
                </label>
              </div>
            </div>

            {sandboxError && (
              <div className="p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 text-xs text-rose-700 dark:text-rose-300 flex items-center gap-2">
                <AlertTriangle size={15} className="text-rose-700 dark:text-rose-400" />
                <span>{sandboxError}</span>
              </div>
            )}

            {/* Results Display */}
            {sandboxResult && (
              <div className="space-y-6">
                {/* Score Comparison Banner */}
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                  <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800">
                    <span className="text-[11px] font-mono text-slate-600 dark:text-zinc-400 block mb-1">INTERNAL KB CONFIDENCE</span>
                    <strong className="text-2xl font-bold text-amber-700 dark:text-amber-400">
                      {sandboxResult.score_comparison?.kb_score_pct || '0%'}
                    </strong>
                    <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-0.5">
                      Threshold: {sandboxResult.internal_confidence?.threshold} ({sandboxResult.internal_confidence?.passes_threshold ? 'PASSED' : 'FALLBACK'})
                    </span>
                  </div>

                  <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800">
                    <span className="text-[11px] font-mono text-slate-600 dark:text-zinc-400 block mb-1">WINNING EVIDENCE SOURCE</span>
                    <div className="flex items-center gap-2 mt-1">
                      <span className="px-2.5 py-1 rounded-full text-xs font-bold uppercase tracking-wider bg-emerald-500/15 text-emerald-700 dark:text-emerald-400 border border-emerald-500/30">
                        {sandboxResult.evidence_selection?.selected_source || 'knowledge_base'}
                      </span>
                    </div>
                    <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-1">
                      Score: {sandboxResult.score_comparison?.winning_score_pct}
                    </span>
                  </div>

                  <div className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800">
                    <span className="text-[11px] font-mono text-slate-600 dark:text-zinc-400 block mb-1">RETRIEVED CANDIDATES</span>
                    <strong className="text-2xl font-bold text-slate-900 dark:text-white">
                      {sandboxResult.retrieval_summary?.total_candidates_retrieved || 0}
                    </strong>
                    <span className="text-[11px] text-slate-600 dark:text-zinc-400 block mt-0.5">
                      Reranked to top {sandboxResult.retrieval_summary?.chunks_after_reranking || 0} chunks
                    </span>
                  </div>
                </div>

                {/* Chunks List */}
                <div className="space-y-3">
                  <h3 className="text-sm font-bold text-slate-900 dark:text-white flex items-center gap-2">
                    <Layers size={16} className="text-amber-700 dark:text-amber-400" />
                    <span>Top Retrieved Chunks (Hidden from regular users)</span>
                  </h3>

                  <div className="space-y-3">
                    {(sandboxResult.top_chunks || []).map((chunk: any, idx: number) => (
                      <div
                        key={idx}
                        className="p-4 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200/90 dark:border-zinc-800/90 hover:border-amber-500/40 transition space-y-2.5"
                      >
                        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                          <div className="flex items-center gap-2">
                            <span className="w-5 h-5 rounded-full bg-amber-500/20 text-amber-700 dark:text-amber-400 flex items-center justify-center font-mono font-bold text-[11px]">
                              {chunk.rank}
                            </span>
                            <strong className="text-slate-900 dark:text-white font-semibold">{chunk.document_name}</strong>
                            {chunk.page_number && (
                              <span className="px-2 py-0.5 rounded bg-slate-100 dark:bg-zinc-800 text-slate-700 dark:text-zinc-300 font-mono text-[10px]">
                                Page {chunk.page_number}
                              </span>
                            )}
                            {chunk.section && (
                              <span className="text-slate-600 dark:text-zinc-400 text-[11px]">
                                § {chunk.section}
                              </span>
                            )}
                          </div>

                          <div className="flex items-center gap-2 font-mono text-[11px]">
                            <span className="text-slate-600 dark:text-zinc-400">Hybrid: <strong className="text-slate-800 dark:text-zinc-200">{chunk.hybrid_score}</strong></span>
                            <span className="text-amber-700 dark:text-amber-400 font-bold">Rerank: {chunk.rerank_score}</span>
                          </div>
                        </div>

                        <p className="text-xs text-slate-700 dark:text-zinc-300 leading-relaxed font-sans bg-white dark:bg-[#121520] p-3 rounded-xl border border-slate-200 dark:border-zinc-800">
                          {chunk.snippet}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </main>
        )}

        {/* Tab 4: System Architecture & Security Policy */}
        {activeTab === 'system' && (
          <main className="flex-1 max-w-7xl w-full mx-auto p-4 md:p-8 space-y-6">
            <div className="pb-4 border-b border-slate-200 dark:border-zinc-800">
              <div className="flex items-center gap-2.5 mb-1">
                <span className="p-2 rounded-xl bg-amber-500/10 text-amber-700 dark:text-amber-400 border border-amber-500/20">
                  <Cpu size={22} strokeWidth={2.2} />
                </span>
                <h1 className="text-2xl font-black tracking-tight text-slate-900 dark:text-white">
                  System Architecture & Security Policy
                </h1>
              </div>
              <p className="text-xs md:text-sm text-slate-600 dark:text-zinc-400">
                Backend security posture, access control enforcement status, and enterprise database telemetry.
              </p>
            </div>

            <AuthenticationControls />

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Security Enforcement Card */}
              <div className="p-6 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800 space-y-4">
                <div className="flex items-center gap-2 text-emerald-700 dark:text-emerald-400 font-semibold text-sm">
                  <ShieldCheck size={18} />
                  <span>Access Control & Data Isolation</span>
                </div>

                <div className="space-y-3 text-xs text-slate-700 dark:text-zinc-300 leading-relaxed">
                  <div className="flex items-start gap-2.5">
                    <Check size={15} className="text-emerald-700 dark:text-emerald-400 shrink-0 mt-0.5" />
                    <div>
                      <strong className="text-slate-900 dark:text-white block">Sources Section Removed from User UI</strong>
                      <span>Regular chat users do not see citations, web source URLs, technical source type badges, or internal document titles.</span>
                    </div>
                  </div>

                  <div className="flex items-start gap-2.5">
                    <Check size={15} className="text-emerald-700 dark:text-emerald-400 shrink-0 mt-0.5" />
                    <div>
                      <strong className="text-slate-900 dark:text-white block">Backend Permissions Enforced</strong>
                      <span>All endpoints under <code>/documents/*</code> and <code>/rag-inspect/*</code> strictly require authenticated administrator credentials or valid admin API key. Standard user tokens receive 403 Forbidden.</span>
                    </div>
                  </div>

                  <div className="flex items-start gap-2.5">
                    <Check size={15} className="text-emerald-700 dark:text-emerald-400 shrink-0 mt-0.5" />
                    <div>
                      <strong className="text-slate-900 dark:text-white block">API Response Sanitization</strong>
                      <span>In <code>/chats/{'{chat_id}'}/messages</code>, citations and source metadata are stripped from the response payload for all non-admin users.</span>
                    </div>
                  </div>
                </div>
              </div>

              {/* Vector & RAG Parameters Card */}
              <div className="p-6 rounded-2xl bg-white dark:bg-[#0d1018] border border-slate-200 dark:border-zinc-800 space-y-4">
                <div className="flex items-center gap-2 text-amber-700 dark:text-amber-400 font-semibold text-sm">
                  <Server size={18} />
                  <span>Vector & Model Configuration</span>
                </div>

                {systemError ? (
                  <p className="text-xs text-rose-700 dark:text-rose-300">{systemError}</p>
                ) : !systemConfig ? (
                  <p className="text-xs text-slate-500 dark:text-zinc-500">Loading configuration...</p>
                ) : (
                  <div className="space-y-2.5 text-xs text-slate-700 dark:text-zinc-300 font-mono">
                    {[
                      ['Embedding Model:', systemConfig.embedding_model],
                      ['Vector Dimensions:', `${systemConfig.embedding_dim}`],
                      ['Answer Model:', `${systemConfig.llm_model} (${systemConfig.llm_provider})`],
                      ['Similarity Threshold:', systemConfig.similarity_threshold.toFixed(2)],
                      ['Internal Evidence Threshold:', systemConfig.internal_evidence_threshold.toFixed(2)],
                      ['Grounding Threshold:', systemConfig.grounding_threshold.toFixed(2)],
                      ['Admin API Key:', systemConfig.admin_api_key_enabled ? 'Enabled' : 'Disabled'],
                    ].map(([label, value]) => (
                      <div key={label} className="flex justify-between gap-4 py-1.5 border-b border-slate-200 dark:border-zinc-800">
                        <span className="text-slate-500 dark:text-zinc-500">{label}</span>
                        <span className="text-slate-900 dark:text-white font-semibold text-right break-all">{value}</span>
                      </div>
                    ))}
                    <div className="flex justify-between gap-4 py-1.5">
                      <span className="text-slate-500 dark:text-zinc-500">Signed-in Administrator:</span>
                      <span className="text-amber-700 dark:text-amber-400 font-semibold text-right break-all">{systemConfig.signed_in_admin}</span>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </main>
        )}
      </div>
    </>
  );
};
