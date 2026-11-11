import React, { useState, useEffect, useRef } from 'react';
import {
  Database,
  FileText,
  UploadCloud,
  CheckCircle2,
  Clock,
  AlertTriangle,
  RefreshCw,
  Trash2,
  Search,
  Play,
  Eye,
  X,
  Layers,
  ShieldCheck,
  Check,
  FileCode,
  FileSpreadsheet,
  Image as ImageIcon,
  File,
  Activity,
  Terminal,
} from 'lucide-react';
import {
  apiGetDocuments,
  apiUploadDocuments,
  apiProcessAllDocuments,
  apiProcessIndividualDocument,
  apiGetProcessingStatus,
  apiDeleteDocument,
  apiGetDocumentDetails,
  connectProcessingStream,
} from '../services/api';
import { bulkDeleteDocuments } from '../services/bulkDeleteDocuments';
import type { KBDocument, DocumentCounts, ProcessingProgress } from '../types';
import { useAuth } from '../context/AuthContext';
import { Navigate } from 'react-router-dom';
import { useRefreshSpinner } from '../components/ui/useRefreshSpinner';

export const DocumentManagementPage: React.FC = () => {
  const { user } = useAuth();

  // Document state
  const [documents, setDocuments] = useState<KBDocument[]>([]);

  const [counts, setCounts] = useState<DocumentCounts>({
    all: 0,
    pending: 0,
    processing: 0,
    completed: 0,
    failed: 0,
    total_chunks: 0,
  });
  const [activeTab, setActiveTab] = useState<'all' | 'pending' | 'processing' | 'completed' | 'failed'>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const refreshSpinner = useRefreshSpinner();

  // Upload state
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadNotice, setUploadNotice] = useState<{ type: 'success' | 'error'; message: string } | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Live Processing state
  const [progress, setProgress] = useState<ProcessingProgress>({
    is_processing: false,
    total_selected: 0,
    completed_count: 0,
    processing_count: 0,
    pending_count: 0,
    failed_count: 0,
    total_chunks_generated: 0,
    total_embeddings_stored: 0,
    overall_progress_pct: 0,
    current_stage: 'Idle',
    verification_status: 'Pending',
    summary: null,
    recent_logs: [],
  });
  const [isProcessingAction, setIsProcessingAction] = useState(false);
  const [showLogsDrawer, setShowLogsDrawer] = useState(false);
  const [showSummaryModal, setShowSummaryModal] = useState(false);

  // Delete modal state
  const [docToDelete, setDocToDelete] = useState<KBDocument | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  // Bulk delete state: ids ticked in the table; only rows currently listed can be deleted.
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [showBulkDelete, setShowBulkDelete] = useState(false);
  const [isBulkDeleting, setIsBulkDeleting] = useState(false);

  // Inspect modal state
  const [docToInspect, setDocToInspect] = useState<any | null>(null);

  // Notification / Toast
  const [toast, setToast] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);

  const showToast = (type: 'success' | 'error' | 'info', message: string) => {
    setToast({ type, message });
    setTimeout(() => {
      setToast(null);
    }, 4500);
  };

  // Load documents
  const fetchDocuments = async (tab = activeTab, search = searchQuery, silent = false) => {
    if (!silent) setLoading(true);
    else setIsRefreshing(true);
    try {
      const data = await apiGetDocuments({
        status: tab,
        search: search.trim() || undefined,
        page: 1,
        page_size: 100,
      });
      setDocuments(data.documents);
      setCounts(data.counts);
    } catch (err: any) {
      console.error('Failed to load documents:', err);
      showToast('error', err.message || 'Could not fetch documents');
    } finally {
      setLoading(false);
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    fetchDocuments(activeTab, searchQuery);
  }, [activeTab]);

  // Initial fetch of processing status and live SSE subscription
  useEffect(() => {
    apiGetProcessingStatus()
      .then((status) => {
        if (status) {
          setProgress(status);
          if (status.summary && !status.is_processing) {
            // Keep last summary
          }
        }
      })
      .catch((err) => console.log('Processing status check:', err));

    // Connect Server-Sent Events stream for real-time progress
    const unsubscribeStream = connectProcessingStream(
      (newProgress) => {
        setProgress(newProgress);
        if (newProgress.is_processing) {
          // If actively processing, periodically sync document table
          fetchDocuments(activeTab, searchQuery, true);
        } else if (newProgress.summary) {
          // Finished!
          setShowSummaryModal(true);
          fetchDocuments(activeTab, searchQuery, true);
        }
      },
      (_err) => {
        // SSE error or reconnecting; fallback to polling handled below if needed
      }
    );

    return () => {
      unsubscribeStream();
    };
  }, []);

  // Polling fallback when processing is active
  useEffect(() => {
    if (!progress.is_processing) return;
    const interval = setInterval(async () => {
      try {
        const latest = await apiGetProcessingStatus();
        setProgress(latest);
        if (!latest.is_processing) {
          fetchDocuments(activeTab, searchQuery, true);
          if (latest.summary) {
            setShowSummaryModal(true);
          }
        }
      } catch {
        // ignore
      }
    }, 1500);
    return () => clearInterval(interval);
  }, [progress.is_processing, activeTab, searchQuery]);

  // File picker / drag & drop handlers
  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files.length > 0) {
      const incoming = Array.from(e.target.files);
      setSelectedFiles((prev) => [...prev, ...incoming]);
      setUploadNotice(null);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const incoming = Array.from(e.dataTransfer.files);
      setSelectedFiles((prev) => [...prev, ...incoming]);
      setUploadNotice(null);
    }
  };

  const removeSelectedFile = (idx: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleUploadSubmit = async () => {
    if (selectedFiles.length === 0) return;
    setIsUploading(true);
    setUploadNotice(null);
    try {
      const result = await apiUploadDocuments(selectedFiles);
      if (result.registered_count > 0) {
        setUploadNotice({
          type: 'success',
          message: `Successfully registered ${result.registered_count} document(s) in Pending status. Click "Process All Documents" to chunk, embed, and index them.`,
        });
        showToast('success', `Registered ${result.registered_count} document(s)`);
        setSelectedFiles([]);
        fetchDocuments(activeTab, searchQuery, true);
      } else if (result.errors?.length > 0) {
        setUploadNotice({
          type: 'error',
          message: `Upload error: ${result.errors[0]?.error}`,
        });
      }
    } catch (err: any) {
      setUploadNotice({
        type: 'error',
        message: err.message || 'Failed to upload files',
      });
      showToast('error', err.message || 'Upload failed');
    } finally {
      setIsUploading(false);
    }
  };

  // Process All Documents
  const handleProcessAll = async () => {
    if (progress.is_processing) return;
    setIsProcessingAction(true);
    try {
      await apiProcessAllDocuments(false);
      showToast('info', 'Batch processing initiated on backend. Monitoring live progress...');
      const p = await apiGetProcessingStatus();
      setProgress(p);
      fetchDocuments(activeTab, searchQuery, true);
    } catch (err: any) {
      showToast('error', err.message || 'Failed to trigger document processing');
    } finally {
      setIsProcessingAction(false);
    }
  };

  // Process Individual Document
  const handleProcessIndividual = async (docId: string, docName: string) => {
    try {
      await apiProcessIndividualDocument(docId, true);
      showToast('info', `Started processing '${docName}'...`);
      const p = await apiGetProcessingStatus();
      setProgress(p);
      fetchDocuments(activeTab, searchQuery, true);
    } catch (err: any) {
      showToast('error', err.message || 'Failed to start processing');
    }
  };

  // Delete Document
  const confirmDelete = async () => {
    if (!docToDelete) return;
    setIsDeleting(true);
    try {
      const res = await apiDeleteDocument(docToDelete.id);
      showToast('success', `Deleted '${docToDelete.filename}' (${res.deleted_chunks} vectors purged).`);
      setDocToDelete(null);
      fetchDocuments(activeTab, searchQuery, true);
    } catch (err: any) {
      showToast('error', err.message || 'Failed to delete document');
    } finally {
      setIsDeleting(false);
    }
  };

  // Bulk selection: drop ticks for rows no longer listed (tab change, search, deletion, refresh).
  useEffect(() => {
    setSelectedDocIds((prev) => {
      const listed = new Set(documents.map((d) => d.id));
      const kept = [...prev].filter((id) => listed.has(id));
      return kept.length === prev.size ? prev : new Set(kept);
    });
  }, [documents]);

  const selectedDocs = documents.filter((d) => selectedDocIds.has(d.id));
  const allListedSelected = documents.length > 0 && selectedDocs.length === documents.length;

  const toggleSelectDoc = (docId: string) => {
    setSelectedDocIds((prev) => {
      const next = new Set(prev);
      if (next.has(docId)) next.delete(docId);
      else next.add(docId);
      return next;
    });
  };

  const toggleSelectAllListed = () => {
    setSelectedDocIds(allListedSelected ? new Set() : new Set(documents.map((d) => d.id)));
  };

  // Bulk Delete Selected Documents
  const confirmBulkDelete = async () => {
    if (selectedDocs.length === 0) return;
    setIsBulkDeleting(true);
    try {
      const result = await bulkDeleteDocuments(selectedDocs, apiDeleteDocument);
      if (result.failed.length === 0) {
        showToast('success', `Deleted ${result.deletedIds.length} document(s) (${result.deletedChunks} vectors purged).`);
      } else {
        showToast(
          'error',
          `Deleted ${result.deletedIds.length} of ${selectedDocs.length} document(s). Failed: ${result.failed
            .map((f) => f.filename)
            .join(', ')}`,
        );
      }
      setSelectedDocIds(new Set(result.failed.map((f) => f.id)));
      setShowBulkDelete(false);
      fetchDocuments(activeTab, searchQuery, true);
    } finally {
      setIsBulkDeleting(false);
    }
  };

  // Inspect Document
  const handleInspect = async (docId: string) => {
    try {
      const details = await apiGetDocumentDetails(docId);
      setDocToInspect(details);
    } catch (err: any) {
      showToast('error', err.message || 'Failed to load details');
    }
  };

  // Helper icon for file types
  const getFileIcon = (fileType: string) => {
    const t = fileType.toLowerCase();
    if (t === 'pdf') return <FileText className="text-red-500" size={18} />;
    if (['png', 'jpg', 'jpeg', 'webp', 'bmp'].includes(t)) return <ImageIcon className="text-blue-500" size={18} />;
    if (['csv', 'tsv', 'json'].includes(t)) return <FileSpreadsheet className="text-emerald-500" size={18} />;
    if (['md', 'txt', 'log'].includes(t)) return <FileCode className="text-amber-500" size={18} />;
    return <File className="text-slate-400" size={18} />;
  };

  // Helper status badge
  const renderStatusBadge = (status: string, _stage?: string) => {
    const s = (status || '').toLowerCase();
    if (s === 'completed') {
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-400 border border-emerald-200 dark:border-emerald-800/60 shadow-xs">
          <CheckCircle2 size={13} className="shrink-0 text-emerald-500" />
          Completed
        </span>
      );
    }
    if (s === 'processing') {
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-blue-50 dark:bg-blue-950/40 text-blue-700 dark:text-blue-400 border border-blue-200 dark:border-blue-800/60 shadow-xs">
          <RefreshCw size={13} className="shrink-0 text-blue-500 animate-spin" />
          Processing
        </span>
      );
    }
    if (s === 'pending') {
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-amber-50 dark:bg-amber-950/40 text-amber-700 dark:text-amber-400 border border-amber-200 dark:border-amber-800/60 shadow-xs">
          <Clock size={13} className="shrink-0 text-amber-500" />
          Pending
        </span>
      );
    }
    if (s === 'failed') {
      return (
        <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-rose-50 dark:bg-rose-950/40 text-rose-700 dark:text-rose-400 border border-rose-200 dark:border-rose-800/60 shadow-xs">
          <AlertTriangle size={13} className="shrink-0 text-rose-500" />
          Failed
        </span>
      );
    }
    return <span className="text-xs text-slate-500">{status}</span>;
  };

  const pendingOrFailedCount = counts.pending + counts.failed;

  if (!user?.is_admin) {
    return <Navigate to="/admin/login" replace />;
  }

  return (
    <div className="flex-1 overflow-y-auto bg-slate-50 dark:bg-zinc-950 text-slate-900 dark:text-zinc-100 p-4 md:p-8">
      {/* Toast Notification */}
      {toast && (
        <div
          className={`fixed top-6 right-6 z-50 flex items-center gap-2 px-4 py-3 rounded-xl shadow-lg border text-sm font-medium transition-all transform animate-in fade-in slide-in-from-top-2 ${
            toast.type === 'success'
              ? 'bg-emerald-50 dark:bg-emerald-950 border-emerald-300 dark:border-emerald-700 text-emerald-800 dark:text-emerald-200'
              : toast.type === 'error'
              ? 'bg-rose-50 dark:bg-rose-950 border-rose-300 dark:border-rose-700 text-rose-800 dark:text-rose-200'
              : 'bg-blue-50 dark:bg-blue-950 border-blue-300 dark:border-blue-700 text-blue-800 dark:text-blue-200'
          }`}
        >
          {toast.type === 'success' ? (
            <CheckCircle2 size={18} />
          ) : toast.type === 'error' ? (
            <AlertTriangle size={18} />
          ) : (
            <Activity size={18} />
          )}
          <span>{toast.message}</span>
          <button
            onClick={() => setToast(null)}
            className="ml-2 hover:opacity-75 text-slate-400"
          >
            <X size={14} />
          </button>
        </div>
      )}

      <div className="max-w-7xl mx-auto space-y-6">
        {/* Header Section */}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pb-4 border-b border-slate-200 dark:border-zinc-800">
          <div>
            <div className="flex items-center gap-2.5 mb-1">
              <span className="p-2 rounded-xl bg-orange-500/10 text-orange-600 dark:text-orange-400 border border-orange-500/20">
                <Database size={22} strokeWidth={2.2} />
              </span>
              <h1 className="text-2xl font-black tracking-tight text-slate-900 dark:text-white">
                Document Management & Vector Knowledge Base
              </h1>
            </div>
            <p className="text-xs md:text-sm text-slate-500 dark:text-zinc-400">
              Manage enterprise documents, run OCR and chunking pipelines, monitor live vector indexing, and synchronize with the RAG knowledge retrieval engine.
            </p>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={() => { refreshSpinner.start(); void fetchDocuments(activeTab, searchQuery, true).finally(refreshSpinner.stop); }}
              disabled={isRefreshing || refreshSpinner.spinning}
              className="inline-flex items-center gap-1.5 px-3 py-2 text-xs font-semibold rounded-xl border border-slate-300 dark:border-zinc-700 hover:bg-slate-100 dark:hover:bg-zinc-800 text-slate-700 dark:text-zinc-300 transition"
              title="Refresh document status"
            >
              <RefreshCw size={14} className={isRefreshing || refreshSpinner.spinning ? 'animate-spin' : ''} />
              Refresh
            </button>

            <button
              onClick={handleProcessAll}
              disabled={progress.is_processing || isProcessingAction || pendingOrFailedCount === 0}
              className={`inline-flex items-center gap-2 px-4 py-2.5 text-xs font-bold rounded-xl shadow-md transition ${
                progress.is_processing || pendingOrFailedCount === 0
                  ? 'bg-slate-200 dark:bg-zinc-800 text-slate-400 dark:text-zinc-600 cursor-not-allowed border border-slate-300 dark:border-zinc-700'
                  : 'bg-gradient-to-r from-orange-500 to-amber-600 hover:from-orange-600 hover:to-amber-700 text-white shadow-orange-500/20 cursor-pointer active:scale-95'
              }`}
            >
              {progress.is_processing ? (
                <>
                  <RefreshCw size={15} className="animate-spin" />
                  Processing in Progress ({progress.overall_progress_pct}%)
                </>
              ) : (
                <>
                  <Play size={15} className="fill-current" />
                  Process All Documents {pendingOrFailedCount > 0 ? `(${pendingOrFailedCount} Pending)` : ''}
                </>
              )}
            </button>
          </div>
        </div>

        {/* Global Summary Stats Cards */}
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <div className="p-4 rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs flex flex-col justify-between">
            <span className="text-[11px] font-bold text-slate-500 dark:text-zinc-400 uppercase tracking-wider">
              Total Documents
            </span>
            <div className="flex items-baseline justify-between mt-2">
              <span className="text-2xl font-black text-slate-900 dark:text-white">
                {counts.all}
              </span>
              <FileText size={18} className="text-slate-400" />
            </div>
          </div>

          <div className="p-4 rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs flex flex-col justify-between">
            <span className="text-[11px] font-bold text-emerald-600 dark:text-emerald-400 uppercase tracking-wider">
              Completed
            </span>
            <div className="flex items-baseline justify-between mt-2">
              <span className="text-2xl font-black text-emerald-600 dark:text-emerald-400">
                {counts.completed}
              </span>
              <CheckCircle2 size={18} className="text-emerald-500" />
            </div>
          </div>

          <div className="p-4 rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs flex flex-col justify-between">
            <span className="text-[11px] font-bold text-amber-600 dark:text-amber-400 uppercase tracking-wider">
              Pending
            </span>
            <div className="flex items-baseline justify-between mt-2">
              <span className="text-2xl font-black text-amber-600 dark:text-amber-400">
                {counts.pending}
              </span>
              <Clock size={18} className="text-amber-500" />
            </div>
          </div>

          <div className="p-4 rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs flex flex-col justify-between">
            <span className="text-[11px] font-bold text-blue-600 dark:text-blue-400 uppercase tracking-wider">
              Processing
            </span>
            <div className="flex items-baseline justify-between mt-2">
              <span className="text-2xl font-black text-blue-600 dark:text-blue-400">
                {counts.processing}
              </span>
              <RefreshCw size={18} className="text-blue-500" />
            </div>
          </div>

          <div className="p-4 rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs flex flex-col justify-between">
            <span className="text-[11px] font-bold text-rose-600 dark:text-rose-400 uppercase tracking-wider">
              Failed
            </span>
            <div className="flex items-baseline justify-between mt-2">
              <span className="text-2xl font-black text-rose-600 dark:text-rose-400">
                {counts.failed}
              </span>
              <AlertTriangle size={18} className="text-rose-500" />
            </div>
          </div>

          <div className="p-4 rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs flex flex-col justify-between">
            <span className="text-[11px] font-bold text-orange-600 dark:text-orange-400 uppercase tracking-wider">
              Total Chunks
            </span>
            <div className="flex items-baseline justify-between mt-2">
              <span className="text-2xl font-black text-orange-600 dark:text-orange-400">
                {counts.total_chunks.toLocaleString()}
              </span>
              <Layers size={18} className="text-orange-500" />
            </div>
          </div>
        </div>

        {/* Live Processing Dashboard Card (Shown when processing or when progress is available) */}
        {(progress.is_processing || (progress.total_selected > 0 && progress.overall_progress_pct < 100)) && (
          <div className="rounded-2xl bg-gradient-to-br from-orange-500/10 via-amber-500/5 to-transparent border border-orange-500/30 dark:border-orange-500/20 p-5 shadow-sm space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div className="flex items-center gap-3">
                <span className="relative flex h-3 w-3">
                  <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-orange-400 opacity-75"></span>
                  <span className="relative inline-flex rounded-full h-3 w-3 bg-orange-500"></span>
                </span>
                <div>
                  <h3 className="text-sm font-bold text-slate-900 dark:text-white flex items-center gap-2">
                    Live Processing Pipeline Active
                    <span className="text-xs px-2 py-0.5 rounded-full bg-orange-500/20 text-orange-600 dark:text-orange-400 font-semibold">
                      {progress.overall_progress_pct}% Completed
                    </span>
                  </h3>
                  <p className="text-xs text-slate-600 dark:text-zinc-400 mt-0.5">
                    Current stage: <strong className="text-orange-600 dark:text-orange-400">{progress.current_stage}</strong>
                    {progress.current_document_name && (
                      <span className="ml-2 font-mono text-[11px] text-slate-500 dark:text-zinc-400 bg-white/60 dark:bg-zinc-800/60 px-1.5 py-0.5 rounded">
                        ({progress.current_document_name})
                      </span>
                    )}
                  </p>
                </div>
              </div>

              <button
                onClick={() => setShowLogsDrawer(!showLogsDrawer)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-semibold rounded-lg bg-white/80 dark:bg-zinc-800/80 border border-slate-300 dark:border-zinc-700 hover:bg-white dark:hover:bg-zinc-800 transition"
              >
                <Terminal size={13} />
                {showLogsDrawer ? 'Hide Live Logs' : 'View Live Logs'}
              </button>
            </div>

            {/* Overall Progress Bar */}
            <div className="space-y-1.5">
              <div className="h-3 w-full bg-slate-200 dark:bg-zinc-800 rounded-full overflow-hidden p-0.5">
                <div
                  className="h-full bg-gradient-to-r from-orange-500 via-amber-500 to-emerald-500 rounded-full transition-all duration-300 ease-out"
                  style={{ width: `${Math.max(5, progress.overall_progress_pct)}%` }}
                />
              </div>
            </div>

            {/* Live Metrics Grid */}
            <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-2 pt-1 text-xs">
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-slate-500 dark:text-zinc-400 uppercase font-semibold">Selected</span>
                <p className="font-bold text-sm text-slate-900 dark:text-white mt-0.5">{progress.total_selected}</p>
              </div>
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-emerald-600 dark:text-emerald-400 uppercase font-semibold">Completed</span>
                <p className="font-bold text-sm text-emerald-600 dark:text-emerald-400 mt-0.5">{progress.completed_count}</p>
              </div>
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-blue-600 dark:text-blue-400 uppercase font-semibold">Processing</span>
                <p className="font-bold text-sm text-blue-600 dark:text-blue-400 mt-0.5">{progress.processing_count}</p>
              </div>
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-amber-600 dark:text-amber-400 uppercase font-semibold">Pending</span>
                <p className="font-bold text-sm text-amber-600 dark:text-amber-400 mt-0.5">{progress.pending_count}</p>
              </div>
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-rose-600 dark:text-rose-400 uppercase font-semibold">Failed</span>
                <p className="font-bold text-sm text-rose-600 dark:text-rose-400 mt-0.5">{progress.failed_count}</p>
              </div>
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-slate-500 dark:text-zinc-400 uppercase font-semibold">Chunks</span>
                <p className="font-bold text-sm text-slate-900 dark:text-white mt-0.5">{progress.total_chunks_generated}</p>
              </div>
              <div className="p-2.5 rounded-xl bg-white/70 dark:bg-zinc-900/70 border border-slate-200 dark:border-zinc-800">
                <span className="text-[10px] text-slate-500 dark:text-zinc-400 uppercase font-semibold">Vectors Stored</span>
                <p className="font-bold text-sm text-slate-900 dark:text-white mt-0.5">{progress.total_embeddings_stored}</p>
              </div>
            </div>

            {/* Live Logs Collapsible Terminal */}
            {showLogsDrawer && progress.recent_logs && progress.recent_logs.length > 0 && (
              <div className="mt-3 p-3 rounded-xl bg-black/90 font-mono text-[11px] text-emerald-400 border border-zinc-800 max-h-48 overflow-y-auto space-y-1">
                {progress.recent_logs.map((log, i) => (
                  <div key={i} className="leading-relaxed">
                    {log}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Upload Section Card */}
        <div className="rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 p-5 shadow-xs space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-base font-bold text-slate-900 dark:text-white flex items-center gap-2">
                <UploadCloud size={18} className="text-orange-500" />
                Upload New Documents to Knowledge Base
              </h2>
              <p className="text-xs text-slate-500 dark:text-zinc-400">
                Supported formats: PDF, DOCX, PNG/JPG (with Optical Character Recognition), Markdown, TXT, CSV, JSON.
              </p>
            </div>
            {selectedFiles.length > 0 && (
              <button
                onClick={handleUploadSubmit}
                disabled={isUploading}
                className="inline-flex items-center gap-2 px-4 py-2 text-xs font-bold rounded-xl bg-orange-500 hover:bg-orange-600 text-white shadow-md shadow-orange-500/20 cursor-pointer transition active:scale-95 disabled:opacity-50"
              >
                {isUploading ? (
                  <>
                    <RefreshCw size={13} className="animate-spin" />
                    Registering Files...
                  </>
                ) : (
                  <>
                    <UploadCloud size={14} />
                    Register {selectedFiles.length} Document(s)
                  </>
                )}
              </button>
            )}
          </div>

          {/* Drag & Drop Zone */}
          <div
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-2xl p-6 text-center cursor-pointer transition-all ${
              isDragging
                ? 'border-orange-500 bg-orange-50/50 dark:bg-orange-950/20 scale-[0.99]'
                : 'border-slate-300 dark:border-zinc-700/80 hover:border-orange-400 dark:hover:border-orange-500/50 bg-slate-50/50 dark:bg-zinc-950/30'
            }`}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              onChange={handleFileChange}
              accept=".pdf,.docx,.png,.jpg,.jpeg,.webp,.bmp,.txt,.md,.markdown,.csv,.json,.log"
              className="hidden"
            />
            <div className="flex flex-col items-center justify-center gap-2">
              <span className="p-3 rounded-full bg-orange-500/10 text-orange-500 dark:text-orange-400">
                <UploadCloud size={24} />
              </span>
              <p className="text-sm font-semibold text-slate-800 dark:text-zinc-200">
                Drag and drop multiple documents here, or <span className="text-orange-500 underline">browse files</span>
              </p>
              <p className="text-[11px] text-slate-400 dark:text-zinc-500">
                New files are safely registered as Pending without overwriting existing documents.
              </p>
            </div>
          </div>

          {/* Selected File Badges */}
          {selectedFiles.length > 0 && (
            <div className="space-y-2 pt-2">
              <div className="flex items-center justify-between text-xs font-semibold text-slate-500 dark:text-zinc-400">
                <span>Selected Files ({selectedFiles.length})</span>
                <button
                  onClick={() => setSelectedFiles([])}
                  className="text-rose-500 hover:underline"
                >
                  Clear all
                </button>
              </div>
              <div className="flex flex-wrap gap-2 max-h-36 overflow-y-auto p-1">
                {selectedFiles.map((f, idx) => (
                  <span
                    key={idx}
                    className="inline-flex items-center gap-2 px-3 py-1.5 rounded-xl bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700 text-xs font-medium text-slate-800 dark:text-zinc-200"
                  >
                    <FileText size={13} className="text-slate-400" />
                    <span className="max-w-[200px] truncate">{f.name}</span>
                    <span className="text-[10px] text-slate-400">
                      ({(f.size / 1024).toFixed(0)} KB)
                    </span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        removeSelectedFile(idx);
                      }}
                      className="text-slate-400 hover:text-rose-500"
                    >
                      <X size={12} />
                    </button>
                  </span>
                ))}
              </div>
            </div>
          )}

          {/* Upload Notice Banner */}
          {uploadNotice && (
            <div
              className={`p-3.5 rounded-xl text-xs font-medium flex items-center justify-between ${
                uploadNotice.type === 'success'
                  ? 'bg-emerald-50 dark:bg-emerald-950/40 text-emerald-800 dark:text-emerald-300 border border-emerald-200 dark:border-emerald-800'
                  : 'bg-rose-50 dark:bg-rose-950/40 text-rose-800 dark:text-rose-300 border border-rose-200 dark:border-rose-800'
              }`}
            >
              <span>{uploadNotice.message}</span>
              <button onClick={() => setUploadNotice(null)} className="hover:opacity-75">
                <X size={14} />
              </button>
            </div>
          )}
        </div>

        {/* Document Table Section */}
        <div className="rounded-2xl bg-white dark:bg-zinc-900 border border-slate-200/80 dark:border-zinc-800 shadow-xs overflow-hidden">
          {/* Controls: Tabs & Search */}
          <div className="p-4 border-b border-slate-200 dark:border-zinc-800 flex flex-col md:flex-row md:items-center justify-between gap-3 bg-slate-50/50 dark:bg-zinc-950/30">
            {/* Category Filter Tabs */}
            <div className="flex items-center gap-1 overflow-x-auto pb-1 md:pb-0">
              {(
                [
                  { id: 'all', label: 'All Documents', count: counts.all },
                  { id: 'pending', label: 'Pending', count: counts.pending },
                  { id: 'processing', label: 'Processing', count: counts.processing },
                  { id: 'completed', label: 'Completed', count: counts.completed },
                  { id: 'failed', label: 'Failed', count: counts.failed },
                ] as const
              ).map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold transition whitespace-nowrap ${
                    activeTab === tab.id
                      ? 'bg-orange-500 text-white shadow-xs'
                      : 'text-slate-600 dark:text-zinc-400 hover:bg-slate-200/70 dark:hover:bg-zinc-800'
                  }`}
                >
                  {tab.label}
                  <span
                    className={`text-[10px] px-1.5 py-0.2 rounded-full font-bold ${
                      activeTab === tab.id
                        ? 'bg-white/25 text-white'
                        : 'bg-slate-200 dark:bg-zinc-800 text-slate-700 dark:text-zinc-300'
                    }`}
                  >
                    {tab.count}
                  </span>
                </button>
              ))}
            </div>

            {/* Bulk delete + search share the right-hand side so the toolbar stays aligned */}
            <div className="flex flex-col sm:flex-row sm:items-center gap-2 md:justify-end md:shrink-0">
              {/* Bulk Delete Selected Button */}
              {selectedDocs.length > 0 && (
                <button
                  type="button"
                  onClick={() => setShowBulkDelete(true)}
                  disabled={progress.is_processing}
                  className="inline-flex items-center justify-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold whitespace-nowrap shrink-0 !text-white bg-rose-600 hover:bg-rose-700 text-white shadow-xs transition cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                  title={
                    progress.is_processing
                      ? 'Wait for processing to finish before deleting'
                      : 'Delete all selected documents and purge their vectors'
                  }
                >
                  <Trash2 size={13} />
                  Delete selected ({selectedDocs.length})
                </button>
              )}

              {/* Search Input */}
              <div className="relative w-full sm:w-64 md:w-72">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  type="text"
                  placeholder="Search filename or title..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') fetchDocuments(activeTab, searchQuery);
                  }}
                  className="w-full pl-9 pr-8 py-1.5 text-xs rounded-xl border border-slate-300 dark:border-zinc-700 bg-white dark:bg-zinc-900 text-slate-900 dark:text-white placeholder:text-slate-400 focus:outline-hidden focus:ring-2 focus:ring-orange-500/20 focus:border-orange-500"
                />
                {searchQuery && (
                  <button
                    onClick={() => {
                      setSearchQuery('');
                      fetchDocuments(activeTab, '');
                    }}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                  >
                    <X size={12} />
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* Table */}
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-100/70 dark:bg-zinc-900/90 text-slate-500 dark:text-zinc-400 border-b border-slate-200 dark:border-zinc-800 uppercase font-semibold text-[10px] tracking-wider">
                <tr>
                  <th className="py-3 pl-4 pr-0 w-8">
                    <input
                      type="checkbox"
                      checked={allListedSelected}
                      ref={(el) => {
                        if (el) el.indeterminate = selectedDocs.length > 0 && !allListedSelected;
                      }}
                      onChange={toggleSelectAllListed}
                      disabled={loading || documents.length === 0}
                      aria-label="Select all listed documents"
                      title="Select all listed documents"
                      className="w-3.5 h-3.5 accent-orange-500 cursor-pointer align-middle"
                    />
                  </th>
                  <th className="py-3 px-4">Document / Filename</th>
                  <th className="py-3 px-4">Type & Size</th>
                  <th className="py-3 px-4">Upload Date</th>
                  <th className="py-3 px-4">Processing Status</th>
                  <th className="py-3 px-4 text-center">Chunks</th>
                  <th className="py-3 px-4 text-center">Vector Index</th>
                  <th className="py-3 px-4 text-right">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-zinc-800/60">
                {loading ? (
                  <tr>
                    <td colSpan={8} className="py-12 text-center text-slate-400">
                      <div className="flex flex-col items-center justify-center gap-2">
                        <RefreshCw size={20} className="animate-spin text-orange-500" />
                        <span>Loading knowledge base documents...</span>
                      </div>
                    </td>
                  </tr>
                ) : documents.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="py-12 text-center text-slate-400">
                      <div className="flex flex-col items-center justify-center gap-2">
                        <FileText size={24} className="text-slate-300 dark:text-zinc-700" />
                        <span className="font-semibold text-slate-700 dark:text-zinc-300">
                          No documents found
                        </span>
                        <p className="text-[11px] text-slate-400 max-w-sm">
                          {searchQuery
                            ? `No documents match "${searchQuery}".`
                            : `There are currently no documents in the "${activeTab}" category.`}
                        </p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  documents.map((doc) => (
                    <tr
                      key={doc.id}
                      className="hover:bg-slate-50/80 dark:hover:bg-zinc-800/40 transition group"
                    >
                      {/* Bulk Select */}
                      <td className="py-3.5 pl-4 pr-0 w-8">
                        <input
                          type="checkbox"
                          checked={selectedDocIds.has(doc.id)}
                          onChange={() => toggleSelectDoc(doc.id)}
                          aria-label={`Select ${doc.filename}`}
                          className="w-3.5 h-3.5 accent-orange-500 cursor-pointer align-middle"
                        />
                      </td>

                      {/* Filename & Title */}
                      <td className="py-3.5 px-4">
                        <div className="flex items-center gap-3">
                          <span className="p-2 rounded-xl bg-slate-100 dark:bg-zinc-800 border border-slate-200 dark:border-zinc-700/60 shrink-0">
                            {getFileIcon(doc.file_type)}
                          </span>
                          <div className="min-w-0 max-w-sm">
                            <strong className="block font-bold text-slate-900 dark:text-white truncate">
                              {doc.filename}
                            </strong>
                            <span className="block text-[11px] text-slate-500 dark:text-zinc-400 truncate">
                              {doc.title || doc.filename}
                            </span>
                            {doc.error_message && (
                              <span className="block text-[10px] text-rose-500 font-medium truncate mt-0.5">
                                Error: {doc.error_message}
                              </span>
                            )}
                          </div>
                        </div>
                      </td>

                      {/* Type & Size */}
                      <td className="py-3.5 px-4 whitespace-nowrap">
                        <span className="inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-slate-100 dark:bg-zinc-800 text-slate-700 dark:text-zinc-300 border border-slate-200 dark:border-zinc-700">
                          {doc.file_type}
                        </span>
                        <span className="ml-2 text-slate-500 dark:text-zinc-400">
                          {doc.file_size_formatted || `${doc.file_size_bytes} B`}
                        </span>
                      </td>

                      {/* Upload Date */}
                      <td className="py-3.5 px-4 whitespace-nowrap text-slate-500 dark:text-zinc-400 text-[11px]">
                        {doc.created_at ? new Date(doc.created_at).toLocaleDateString() : '—'}
                      </td>

                      {/* Processing Status */}
                      <td className="py-3.5 px-4 whitespace-nowrap">
                        {renderStatusBadge(doc.status, doc.stage)}
                        <span className="block text-[10px] text-slate-400 dark:text-zinc-500 mt-0.5 truncate max-w-[150px]">
                          {doc.stage}
                        </span>
                      </td>

                      {/* Chunks */}
                      <td className="py-3.5 px-4 text-center whitespace-nowrap">
                        <span className="inline-block px-2.5 py-0.5 rounded-full font-bold text-xs bg-slate-100 dark:bg-zinc-800 text-slate-800 dark:text-zinc-200">
                          {doc.chunk_count}
                        </span>
                      </td>

                      {/* Vector Index Status */}
                      <td className="py-3.5 px-4 text-center whitespace-nowrap">
                        {doc.vector_indexed && doc.chunk_count > 0 ? (
                          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-600 dark:text-emerald-400">
                            <ShieldCheck size={14} className="text-emerald-500" />
                            Indexed
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-slate-400">
                            <Clock size={13} />
                            Unindexed
                          </span>
                        )}
                      </td>

                      {/* Actions */}
                      <td className="py-3.5 px-4 text-right whitespace-nowrap">
                        <div className="inline-flex items-center gap-1.5">
                          {/* Process or Retry Button */}
                          {(doc.status === 'pending' || doc.status === 'failed') && (
                            <button
                              onClick={() => handleProcessIndividual(doc.id, doc.filename)}
                              disabled={progress.is_processing}
                              className="px-2.5 py-1 text-xs font-bold rounded-lg bg-orange-500/10 hover:bg-orange-500/20 text-orange-600 dark:text-orange-400 border border-orange-500/25 transition cursor-pointer"
                              title="Process this document"
                            >
                              <Play size={11} className="inline mr-1 fill-current" />
                              {doc.status === 'failed' ? 'Retry' : 'Process'}
                            </button>
                          )}

                          {/* Inspect Chunks Button */}
                          <button
                            onClick={() => handleInspect(doc.id)}
                            className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-zinc-800 text-slate-500 hover:text-slate-800 dark:hover:text-zinc-200 transition"
                            title="Inspect metadata and chunks"
                          >
                            <Eye size={15} />
                          </button>

                          {/* Delete Document Button */}
                          <button
                            onClick={() => setDocToDelete(doc)}
                            className="p-1.5 rounded-lg hover:bg-rose-50 dark:hover:bg-rose-950/40 text-slate-400 hover:text-rose-600 dark:hover:text-rose-400 transition"
                            title="Delete document and purge vectors"
                          >
                            <Trash2 size={15} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {/* Completion Summary Modal */}
      {showSummaryModal && progress.summary && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white dark:bg-zinc-900 rounded-3xl max-w-md w-full border border-slate-200 dark:border-zinc-800 p-6 shadow-2xl space-y-5 animate-in zoom-in-95">
            <div className="text-center space-y-2">
              <span className="inline-flex p-3.5 rounded-2xl bg-emerald-500/10 text-emerald-500 border border-emerald-500/20">
                <CheckCircle2 size={32} />
              </span>
              <h2 className="text-lg font-black text-slate-900 dark:text-white">
                Document Processing Completed Successfully!
              </h2>
              <p className="text-xs text-slate-500 dark:text-zinc-400">
                All selected knowledge base files have been extracted, chunked, embedded, and stored into the vector database.
              </p>
            </div>

            <div className="bg-slate-50 dark:bg-zinc-950/60 rounded-2xl p-4 border border-slate-200 dark:border-zinc-800 space-y-2.5 text-xs">
              <div className="flex justify-between items-center py-1 border-b border-slate-200/60 dark:border-zinc-800">
                <span className="text-slate-500 dark:text-zinc-400">Documents Processed</span>
                <strong className="font-bold text-slate-900 dark:text-white">
                  {progress.summary.documents_processed}
                </strong>
              </div>
              <div className="flex justify-between items-center py-1 border-b border-slate-200/60 dark:border-zinc-800">
                <span className="text-slate-500 dark:text-zinc-400">Chunks Generated</span>
                <strong className="font-bold text-orange-600 dark:text-orange-400">
                  {progress.summary.chunks_generated.toLocaleString()}
                </strong>
              </div>
              <div className="flex justify-between items-center py-1 border-b border-slate-200/60 dark:border-zinc-800">
                <span className="text-slate-500 dark:text-zinc-400">Embeddings Stored</span>
                <strong className="font-bold text-emerald-600 dark:text-emerald-400">
                  {progress.summary.embeddings_stored.toLocaleString()}
                </strong>
              </div>
              <div className="flex justify-between items-center py-1 border-b border-slate-200/60 dark:border-zinc-800">
                <span className="text-slate-500 dark:text-zinc-400">Failed Documents</span>
                <strong className={`font-bold ${progress.summary.failed_documents > 0 ? 'text-rose-500' : 'text-slate-700 dark:text-zinc-300'}`}>
                  {progress.summary.failed_documents}
                </strong>
              </div>
              <div className="flex justify-between items-center py-1">
                <span className="text-slate-500 dark:text-zinc-400">Vector Database Verification</span>
                <span className="inline-flex items-center gap-1 font-bold text-emerald-600 dark:text-emerald-400">
                  <ShieldCheck size={14} />
                  {progress.summary.vector_database_verification}
                </span>
              </div>
            </div>

            <button
              onClick={() => setShowSummaryModal(false)}
              className="w-full py-2.5 rounded-xl font-bold text-xs bg-orange-600 !text-white hover:bg-orange-500 transition cursor-pointer shadow-md"
            >
              Done & View Knowledge Base
            </button>
          </div>
        </div>
      )}

      {/* Delete Confirmation Modal */}
      {docToDelete && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white dark:bg-zinc-900 rounded-3xl max-w-md w-full border border-slate-200 dark:border-zinc-800 p-6 shadow-2xl space-y-4 animate-in zoom-in-95">
            <div className="flex items-center gap-3 text-rose-600 dark:text-rose-400">
              <span className="p-3 rounded-2xl bg-rose-500/10 border border-rose-500/20">
                <AlertTriangle size={24} />
              </span>
              <div>
                <h3 className="text-base font-bold text-slate-900 dark:text-white">
                  Permanently Delete Document?
                </h3>
                <p className="text-xs text-slate-500 dark:text-zinc-400">
                  This action removes all chunks and synchronized vectors.
                </p>
              </div>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-zinc-950/80 border border-slate-200 dark:border-zinc-800 text-xs space-y-2">
              <div>
                <span className="text-slate-500 dark:text-zinc-400">Document Filename:</span>
                <p className="font-bold text-slate-900 dark:text-white truncate">
                  {docToDelete.filename}
                </p>
              </div>
              <ul className="list-disc pl-4 space-y-1 text-slate-600 dark:text-zinc-400 text-[11px]">
                <li>Document will be unregistered from the knowledge base registry.</li>
                <li>All <strong>{docToDelete.chunk_count} chunk(s) and dense embeddings</strong> will be purged from the vector database.</li>
                <li>The RAG chatbot will immediately cease retrieving this document's content.</li>
                <li>The physical file on disk will be deleted.</li>
              </ul>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                onClick={() => setDocToDelete(null)}
                disabled={isDeleting}
                className="px-4 py-2 text-xs font-semibold rounded-xl border border-slate-300 dark:border-zinc-700 hover:bg-slate-100 dark:hover:bg-zinc-800 text-slate-700 dark:text-zinc-300 transition"
              >
                Cancel
              </button>
              <button
                onClick={confirmDelete}
                disabled={isDeleting}
                className="px-4 py-2 text-xs font-bold rounded-xl bg-rose-600 hover:bg-rose-700 text-white shadow-md shadow-rose-600/20 transition cursor-pointer active:scale-95 disabled:opacity-50"
              >
                {isDeleting ? 'Purging Vectors...' : 'Delete Document & Purge Vectors'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Bulk Delete Confirmation Modal */}
      {showBulkDelete && selectedDocs.length > 0 && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white dark:bg-zinc-900 rounded-3xl max-w-md w-full border border-slate-200 dark:border-zinc-800 p-6 shadow-2xl space-y-4 animate-in zoom-in-95">
            <div className="flex items-center gap-3 text-rose-600 dark:text-rose-400">
              <span className="p-3 rounded-2xl bg-rose-500/10 border border-rose-500/20">
                <AlertTriangle size={24} />
              </span>
              <div>
                <h3 className="text-base font-bold text-slate-900 dark:text-white">
                  Permanently Delete {selectedDocs.length} Document(s)?
                </h3>
                <p className="text-xs text-slate-500 dark:text-zinc-400">
                  This action removes all chunks and synchronized vectors.
                </p>
              </div>
            </div>

            <div className="p-3.5 rounded-xl bg-slate-50 dark:bg-zinc-950/80 border border-slate-200 dark:border-zinc-800 text-xs space-y-2">
              <div>
                <span className="text-slate-500 dark:text-zinc-400">Selected Documents:</span>
                <ul className="mt-1 max-h-32 overflow-y-auto space-y-0.5 pr-1">
                  {selectedDocs.map((d) => (
                    <li key={d.id} className="font-bold text-slate-900 dark:text-white truncate">
                      {d.filename}
                    </li>
                  ))}
                </ul>
              </div>
              <ul className="list-disc pl-4 space-y-1 text-slate-600 dark:text-zinc-400 text-[11px]">
                <li>These documents will be unregistered from the knowledge base registry.</li>
                <li>
                  All <strong>{selectedDocs.reduce((sum, d) => sum + d.chunk_count, 0)} chunk(s) and dense embeddings</strong>{' '}
                  will be purged from the vector database.
                </li>
                <li>The RAG chatbot will immediately cease retrieving their content.</li>
                <li>The physical files on disk will be deleted.</li>
              </ul>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2">
              <button
                onClick={() => setShowBulkDelete(false)}
                disabled={isBulkDeleting}
                className="px-4 py-2 text-xs font-semibold rounded-xl border border-slate-300 dark:border-zinc-700 hover:bg-slate-100 dark:hover:bg-zinc-800 text-slate-700 dark:text-zinc-300 transition"
              >
                Cancel
              </button>
              <button
                onClick={confirmBulkDelete}
                disabled={isBulkDeleting}
                className="px-4 py-2 text-xs font-bold rounded-xl bg-rose-600 hover:bg-rose-700 text-white shadow-md shadow-rose-600/20 transition cursor-pointer active:scale-95 disabled:opacity-50"
              >
                {isBulkDeleting ? 'Purging Vectors...' : `Delete ${selectedDocs.length} Document(s) & Purge Vectors`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Inspect Document Modal */}
      {docToInspect && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-xs animate-in fade-in">
          <div className="bg-white dark:bg-zinc-900 rounded-3xl max-w-2xl w-full max-h-[85vh] flex flex-col border border-slate-200 dark:border-zinc-800 shadow-2xl animate-in zoom-in-95 overflow-hidden">
            <div className="p-5 border-b border-slate-200 dark:border-zinc-800 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <span className="p-2 rounded-xl bg-orange-500/10 text-orange-500">
                  <Database size={20} />
                </span>
                <div>
                  <h3 className="text-sm font-bold text-slate-900 dark:text-white">
                    {docToInspect.filename}
                  </h3>
                  <span className="text-[11px] text-slate-400">
                    ID: {docToInspect.id}
                  </span>
                </div>
              </div>
              <button
                onClick={() => setDocToInspect(null)}
                className="p-1 rounded-lg text-slate-400 hover:text-slate-600 dark:hover:text-zinc-200"
              >
                <X size={18} />
              </button>
            </div>

            <div className="p-5 overflow-y-auto space-y-4 text-xs">
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                <div className="p-3 rounded-xl bg-slate-50 dark:bg-zinc-950 border border-slate-200 dark:border-zinc-800">
                  <span className="text-[10px] text-slate-400 uppercase font-semibold">Status</span>
                  <p className="font-bold mt-0.5">{docToInspect.status}</p>
                </div>
                <div className="p-3 rounded-xl bg-slate-50 dark:bg-zinc-950 border border-slate-200 dark:border-zinc-800">
                  <span className="text-[10px] text-slate-400 uppercase font-semibold">Chunks</span>
                  <p className="font-bold mt-0.5">{docToInspect.chunk_count}</p>
                </div>
                <div className="p-3 rounded-xl bg-slate-50 dark:bg-zinc-950 border border-slate-200 dark:border-zinc-800">
                  <span className="text-[10px] text-slate-400 uppercase font-semibold">Pages</span>
                  <p className="font-bold mt-0.5">{docToInspect.page_count}</p>
                </div>
                <div className="p-3 rounded-xl bg-slate-50 dark:bg-zinc-950 border border-slate-200 dark:border-zinc-800">
                  <span className="text-[10px] text-slate-400 uppercase font-semibold">Vector Indexed</span>
                  <p className="font-bold text-emerald-500 mt-0.5">{docToInspect.vector_indexed ? 'Yes' : 'No'}</p>
                </div>
              </div>

              {docToInspect.file_hash && (
                <div className="text-[11px] font-mono p-2.5 rounded-xl bg-slate-100 dark:bg-zinc-950 text-slate-600 dark:text-zinc-400 border border-slate-200 dark:border-zinc-800 break-all">
                  SHA-256: {docToInspect.file_hash}
                </div>
              )}

              {/* Sample Chunks */}
              <div className="space-y-2">
                <h4 className="font-bold text-slate-900 dark:text-white flex items-center justify-between">
                  <span>Indexed Chunks Preview ({docToInspect.sample_chunks?.length || 0})</span>
                  <span className="text-[10px] font-normal text-slate-400">384-dimensional vectors</span>
                </h4>
                {docToInspect.sample_chunks && docToInspect.sample_chunks.length > 0 ? (
                  <div className="space-y-2 max-h-60 overflow-y-auto pr-1">
                    {docToInspect.sample_chunks.map((c: any) => (
                      <div
                        key={c.id}
                        className="p-3 rounded-xl bg-slate-50 dark:bg-zinc-950 border border-slate-200 dark:border-zinc-800 space-y-1.5"
                      >
                        <div className="flex items-center justify-between text-[10px] text-slate-400 font-semibold">
                          <span>Chunk #{c.chunk_index} (Page {c.page_number})</span>
                          <span className="inline-flex items-center gap-1 text-emerald-500">
                            <Check size={11} />
                            Vector Active
                          </span>
                        </div>
                        <p className="text-slate-700 dark:text-zinc-300 line-clamp-3 text-[11px] leading-relaxed">
                          {c.text_preview}
                        </p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-slate-400 italic text-[11px]">No chunks currently indexed for this document.</p>
                )}
              </div>
            </div>

            <div className="p-4 border-t border-slate-200 dark:border-zinc-800 flex justify-end">
              <button
                onClick={() => setDocToInspect(null)}
                className="px-4 py-2 text-xs font-bold rounded-xl bg-slate-900 dark:bg-white !text-white dark:!text-slate-900 hover:opacity-90 transition cursor-pointer"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
