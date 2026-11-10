import { ModeToggle } from './ModeToggle';
import { shouldFocusInput } from './typeToFocus';
import React, { useState, useRef, useEffect, useCallback } from 'react';
import {
  Send,
  Mic,
  MicOff,
  Square,
  Plus,
  X,
  FileText,
  FileSpreadsheet,
  FileCode,
  Image as ImageIcon,
  FolderOpen,
  Link2,
  Paperclip,
  Loader2,
  CheckCircle2,
  AlertCircle,
} from 'lucide-react';
import { apiExtractDocument, apiExtractUrl } from '../../services/api';
import {
  MAX_ATTACHMENTS,
  collectDropped,
  displayName,
  extractUrls,
  inPool,
  isHttpUrl,
  linkLabel,
  selectReadableFiles,
  type PickedFile,
} from './attachmentIntake';
import type { ChatMode, DocumentAttachment } from '../../types';

interface ChatInputProps {
  onSendMessage: (message: string, attachments?: DocumentAttachment[]) => void;
  onStop?: () => void;
  isLoading: boolean;
  mode: ChatMode;
  onModeChange: (mode: ChatMode) => void;
  /** Text to start with, e.g. a prompt opened from a share link. */
  initialInput?: string;
}

interface LocalAttachment {
  id: string;
  /** The uploaded file; absent for a linked page. */
  file?: File;
  /** The linked address, for a linked page. */
  url?: string;
  name: string;
  size: number;
  file_type: string;
  status: 'extracting' | 'ready' | 'error';
  page_count?: number;
  char_count?: number;
  text?: string;
  preview?: string;
  errorMessage?: string;
  thumbUrl?: string;
  detected_question?: string | null;
  analysis_method?: string | null;
}

const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'bmp'];
const PASTE_IMAGE_TYPES: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
  'image/bmp': 'bmp',
};

const isImageType = (fileType: string) => IMAGE_EXTENSIONS.includes(fileType);

// Extend Window interface for SpeechRecognition browser API
declare global {
  interface Window {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    SpeechRecognition: any;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    webkitSpeechRecognition: any;
  }
}

export const ChatInput: React.FC<ChatInputProps> = ({ onSendMessage, onStop, isLoading, mode, onModeChange, initialInput = '' }) => {
  const [input, setInput] = useState(initialInput);
  const [attachments, setAttachments] = useState<LocalAttachment[]>([]);
  const [showAttachMenu, setShowAttachMenu] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [voiceSupported, setVoiceSupported] = useState(false);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showLinkInput, setShowLinkInput] = useState(false);
  const [linkDraft, setLinkDraft] = useState('');
  const [isReadingLinks, setIsReadingLinks] = useState(false);
  // Links that could not be read: the next send goes ahead without them instead of blocking the question.
  const skippedLinksRef = useRef<Set<string>>(new Set());

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const recognitionRef = useRef<any>(null);
  const interimRef = useRef('');
  const menuRef = useRef<HTMLDivElement>(null);
  const attachButtonRef = useRef<HTMLButtonElement>(null);

  const filesInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  // Close attachment dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (
        menuRef.current &&
        !menuRef.current.contains(event.target as Node) &&
        attachButtonRef.current &&
        !attachButtonRef.current.contains(event.target as Node)
      ) {
        setShowAttachMenu(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Detect browser support for SpeechRecognition
  useEffect(() => {
    const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;
    setVoiceSupported(!!SpeechRecognitionAPI);
  }, []);

  // Auto-resize textarea
  // Typing anywhere on the page starts the question: the key goes to the message box (like ChatGPT).
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const box = textareaRef.current;
      if (!box || box.disabled) return;
      const dialogOpen = document.querySelector('[role="dialog"], [aria-modal="true"]') !== null;
      if (shouldFocusInput(event, dialogOpen)) box.focus(); // the key then lands in the focused box
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 180)}px`;
    }
  }, [input, attachments]);

  const stopListening = useCallback(() => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      recognitionRef.current = null;
    }
    interimRef.current = '';
    setIsListening(false);
  }, []);

  const startListening = useCallback(() => {
    setVoiceError(null);
    const SpeechRecognitionAPI = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognitionAPI) return;

    const recognition = new SpeechRecognitionAPI();
    recognition.lang = 'en-US';
    recognition.interimResults = true;
    recognition.continuous = true;
    recognition.maxAlternatives = 1;

    recognitionRef.current = recognition;

    recognition.onstart = () => {
      setIsListening(true);
    };

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      let finalTranscript = '';
      let interimTranscript = '';

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          finalTranscript += transcript;
        } else {
          interimTranscript += transcript;
        }
      }

      if (finalTranscript) {
        setInput((prev) => {
          const base = prev.endsWith(' ') || prev === '' ? prev : prev + ' ';
          return (base + finalTranscript).trimStart();
        });
      }
      interimRef.current = interimTranscript;
    };

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      if (event.error === 'not-allowed') {
        setVoiceError('Microphone access denied. Please allow microphone permissions.');
      } else if (event.error !== 'aborted') {
        setVoiceError('Voice recognition error. Please try again.');
      }
      stopListening();
    };

    recognition.onend = () => {
      setIsListening(false);
      recognitionRef.current = null;
      interimRef.current = '';
    };

    recognition.start();
  }, [stopListening]);

  const toggleVoice = () => {
    if (isListening) {
      stopListening();
    } else {
      startListening();
    }
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const getFileBadgeColor = (ext: string) => {
    switch (ext) {
      case 'pdf':
        return 'bg-red-500/15 text-red-600 dark:text-red-400 border-red-500/30';
      case 'docx':
      case 'doc':
      case 'rtf':
      case 'odt':
        return 'bg-blue-500/15 text-blue-600 dark:text-blue-400 border-blue-500/30';
      case 'png':
      case 'jpg':
      case 'jpeg':
      case 'webp':
      case 'bmp':
        return 'bg-violet-500/15 text-violet-600 dark:text-violet-400 border-violet-500/30';
      case 'pptx':
      case 'ppt':
      case 'odp':
        return 'bg-orange-500/15 text-orange-600 dark:text-orange-400 border-orange-500/30';
      case 'url':
      case 'html':
      case 'htm':
        return 'bg-sky-500/15 text-sky-600 dark:text-sky-400 border-sky-500/30';
      case 'csv':
      case 'xlsx':
      case 'xlsm':
      case 'xls':
      case 'ods':
        return 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 border-emerald-500/30';
      case 'json':
      case 'xml':
      case 'sql':
        return 'bg-amber-500/15 text-amber-600 dark:text-amber-400 border-amber-500/30';
      default:
        return 'bg-slate-500/15 text-slate-600 dark:text-slate-400 border-slate-500/30';
    }
  };

  const patchAttachment = (id: string, patch: Partial<LocalAttachment>) =>
    setAttachments((prev) => prev.map((a) => (a.id === id ? { ...a, ...patch } : a)));

  const newId = () => `${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;

  const handleFilesSelected = async (picked: PickedFile[]) => {
    if (picked.length === 0) return;
    setShowAttachMenu(false);
    const { accepted, skipped } = selectReadableFiles(picked, MAX_ATTACHMENTS - attachments.length);
    if (skipped > 0) {
      setNotice(
        accepted.length === 0
          ? `Nothing was added: ${skipped} file(s) were hidden, empty, over 10 MB, unreadable binaries, or over the ${MAX_ATTACHMENTS}-file limit.`
          : `Added ${accepted.length} of ${picked.length} files. ${skipped} were skipped (hidden, empty, over 10 MB, unreadable binaries, or over the ${MAX_ATTACHMENTS}-file limit).`
      );
    } else {
      setNotice(null);
    }
    if (accepted.length === 0) return;

    const newItems: LocalAttachment[] = accepted.map(({ file, path }) => {
      const fileType = file.name.split('.').pop()?.toLowerCase() || 'txt';
      return {
        id: newId(),
        file,
        name: displayName(path),
        size: file.size,
        file_type: fileType,
        status: 'extracting',
        thumbUrl: isImageType(fileType) ? URL.createObjectURL(file) : undefined,
      };
    });
    setAttachments((prev) => [...prev, ...newItems]);

    await inPool(newItems, 3, async (item) => {
      try {
        const res = await apiExtractDocument(item.file!);
        patchAttachment(item.id, {
          status: 'ready',
          page_count: res.page_count,
          char_count: res.char_count,
          text: res.text,
          preview: res.preview,
          detected_question: res.detected_question,
          analysis_method: res.analysis_method,
        });
      } catch (err: any) {
        patchAttachment(item.id, { status: 'error', errorMessage: err.message || 'Could not extract text' });
      }
    });
  };

  /** Reads a linked page or online file and adds it like an uploaded document. Resolves to false when it failed. */
  const addLink = async (rawUrl: string): Promise<boolean> => {
    const url = rawUrl.trim();
    if (!isHttpUrl(url)) {
      setNotice('Enter a full web link that starts with http:// or https://');
      return false;
    }
    if (attachments.some((a) => a.url === url)) return true;
    if (attachments.length >= MAX_ATTACHMENTS) {
      setNotice(`You can attach up to ${MAX_ATTACHMENTS} files and links to one message.`);
      return false;
    }
    const id = newId();
    setAttachments((prev) => [
      ...prev,
      { id, url, name: linkLabel(url), size: 0, file_type: 'url', status: 'extracting' },
    ]);
    try {
      const res = await apiExtractUrl(url);
      patchAttachment(id, {
        status: 'ready',
        name: res.filename || linkLabel(url),
        size: res.size_bytes,
        page_count: res.page_count,
        char_count: res.char_count,
        text: res.text,
        preview: res.preview,
      });
      return true;
    } catch (err: any) {
      patchAttachment(id, { status: 'error', errorMessage: err.message || 'Could not read the link' });
      return false;
    }
  };

  const submitLinkDraft = async () => {
    const url = linkDraft.trim();
    if (!url) return;
    setShowAttachMenu(false);
    setShowLinkInput(false);
    setLinkDraft('');
    await addLink(url);
  };

  const handleFileInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = event.currentTarget.files;
    if (selected) {
      // A folder pick reports each file's path inside the folder; a file pick only its name.
      void handleFilesSelected(
        Array.from(selected).map((file) => ({ file, path: file.webkitRelativePath || file.name }))
      );
    }
    // Allow selecting the same file again after removing it or correcting an extraction issue.
    event.currentTarget.value = '';
  };

  // Ctrl+V / Cmd+V of a screenshot attaches it like an uploaded image; plain text pastes normally.
  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const images = Array.from(e.clipboardData.items)
      .filter((item) => item.kind === 'file' && item.type in PASTE_IMAGE_TYPES)
      .map((item) => item.getAsFile())
      .filter((f): f is File => f !== null);
    if (images.length === 0) return;
    e.preventDefault();
    const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
    const named = images.map(
      (f, i) =>
        new File([f], `pasted-image-${stamp}${images.length > 1 ? `-${i + 1}` : ''}.${PASTE_IMAGE_TYPES[f.type]}`, {
          type: f.type,
        })
    );
    void handleFilesSelected(named.map((file) => ({ file, path: file.name })));
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => {
      const removed = prev.find((a) => a.id === id);
      if (removed?.thumbUrl) URL.revokeObjectURL(removed.thumbUrl);
      return prev.filter((a) => a.id !== id);
    });
  };

  const isExtracting = attachments.some((a) => a.status === 'extracting') || isReadingLinks;

  const handleSubmit = async (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    if (isLoading || isExtracting) return;

    // Links typed or pasted in the question are read first, so the answer can use the pages they point to.
    let linkAttachments: LocalAttachment[] = [];
    const typedLinks = extractUrls(input).filter(
      (url) => !attachments.some((a) => a.url === url) && !skippedLinksRef.current.has(url)
    );
    if (typedLinks.length > 0 && attachments.length < MAX_ATTACHMENTS) {
      setIsReadingLinks(true);
      setNotice(null);
      const results = await inPool(typedLinks.slice(0, MAX_ATTACHMENTS - attachments.length), 3, async (url) => {
        try {
          const res = await apiExtractUrl(url);
          return { url, res, error: null as string | null };
        } catch (err: any) {
          return { url, res: null, error: (err.message as string) || 'could not be read' };
        }
      });
      setIsReadingLinks(false);
      const failed = results.filter((r) => r.error);
      if (failed.length > 0) {
        failed.forEach((r) => skippedLinksRef.current.add(r.url));
        setNotice(
          `Could not read ${failed.map((r) => linkLabel(r.url)).join(', ')}: ${failed[0].error} ` +
            'Press send again to ask without reading it, or fix the link.'
        );
        return;
      }
      linkAttachments = results.map(({ url, res }) => ({
        id: newId(),
        url,
        name: res!.filename || linkLabel(url),
        size: res!.size_bytes,
        file_type: 'url',
        status: 'ready' as const,
        page_count: res!.page_count,
        text: res!.text,
        preview: res!.preview,
      }));
    }
    skippedLinksRef.current.clear();
    setNotice(null);

    const readyAttachments: DocumentAttachment[] = [...attachments, ...linkAttachments]
      .filter((a) => a.status === 'ready' && a.text)
      .map((a) => ({
        name: a.name,
        file_type: a.file_type,
        size_bytes: a.size,
        page_count: a.page_count,
        text: a.text!,
        preview: a.preview,
      }));

    const trimmedInput = input.trim();
    if (!trimmedInput && readyAttachments.length === 0) return;

    if (isListening) stopListening();

    const readyImages = attachments.filter((a) => a.status === 'ready' && a.text && isImageType(a.file_type));
    const detectedQuestions = readyImages
      .map((a) => a.detected_question?.trim())
      .filter((q): q is string => !!q);

    let defaultQuery = 'Please analyze the attached document(s), summarizing key sections and explaining core findings.';
    if (readyImages.length > 0 && readyImages.length === readyAttachments.length) {
      defaultQuery = detectedQuestions.length > 0
        ? `Explain what the attached image shows and give clear step-by-step instructions to resolve this: ${detectedQuestions.join(' Also: ')}`
        : 'Explain what the attached image shows. If it contains an error, issue or question, explain the likely cause and give clear step-by-step instructions to fix or answer it.';
    }
    const finalQuery = trimmedInput || defaultQuery;

    onSendMessage(finalQuery, readyAttachments);
    setInput('');
    attachments.forEach((a) => a.thumbUrl && URL.revokeObjectURL(a.thumbUrl));
    setAttachments([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      // Collected before the first await: the browser clears the drop data afterwards. Folders are walked.
      void collectDropped(e.dataTransfer).then((picked) => handleFilesSelected(picked));
    }
  };

  return (
    <div className="w-full max-w-4xl mx-auto px-4 pb-4 pt-2">
      {/* Hidden pickers: any file type (the server decides what it can read), and a whole folder. */}
      <input type="file" ref={filesInputRef} multiple className="hidden" onChange={handleFileInputChange} />
      <input
        type="file"
        ref={folderInputRef}
        multiple
        className="hidden"
        onChange={handleFileInputChange}
        {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
      />

      {notice && (
        <div className="mb-2 px-3 py-2 rounded-xl bg-amber-50 dark:bg-amber-950/40 border border-amber-300/60 dark:border-amber-700/50 text-xs text-amber-800 dark:text-amber-200 flex items-start justify-between gap-2">
          <span>{notice}</span>
          <button type="button" onClick={() => setNotice(null)} className="font-bold shrink-0" aria-label="Dismiss">✕</button>
        </div>
      )}

      {/* Voice error toast */}
      {voiceError && (
        <div className="mb-2 px-3 py-2 rounded-xl bg-rose-950/60 border border-rose-700/50 text-xs text-rose-300 flex items-center justify-between gap-2">
          <span>{voiceError}</span>
          <button onClick={() => setVoiceError(null)} className="text-rose-400 hover:text-rose-200 transition font-bold shrink-0">✕</button>
        </div>
      )}

      {/* Main Input Form with Drag & Drop */}
      <form
        onSubmit={handleSubmit}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={`relative bg-white dark:bg-[#09090e] border rounded-2xl shadow-sm dark:shadow-2xl transition-all duration-200 flex flex-col ${
          isDragging
            ? 'border-orange-500 ring-2 ring-orange-500/30 bg-orange-50/20 dark:bg-orange-950/10'
            : isListening
            ? 'border-rose-500/70 ring-2 ring-rose-500/25'
            : 'border-slate-300 dark:border-[#1f1f2c] focus-within:border-orange-500 focus-within:ring-2 focus-within:ring-orange-500/20 focus-within:shadow-md'
        }`}
      >
        {/* Drag Overlay indicator */}
        {isDragging && (
          <div className="absolute inset-0 z-30 bg-orange-500/10 dark:bg-orange-500/15 backdrop-blur-[2px] rounded-2xl flex items-center justify-center border-2 border-dashed border-orange-500 pointer-events-none">
            <div className="flex items-center gap-2 px-4 py-2 rounded-xl bg-white dark:bg-[#0c0c14] border border-orange-500/40 text-orange-600 dark:text-orange-400 text-xs font-semibold shadow-lg">
              <Plus className="w-4 h-4" />
              <span>Drop documents here to attach</span>
            </div>
          </div>
        )}

        {/* Attachment Preview Chips inside the input container */}
        {attachments.length > 0 && (
          <div className="px-3 pt-3 pb-1 flex flex-wrap gap-2 items-center border-b border-slate-100 dark:border-[#161622]">
            {attachments.map((att) => {
              const isPdf = att.file_type === 'pdf';
              const isDoc = att.file_type === 'docx' || att.file_type === 'doc';
              const isData = ['csv', 'json', 'xlsx'].includes(att.file_type);
              const isImage = isImageType(att.file_type);
              const isLink = att.file_type === 'url';

              return (
                <div
                  key={att.id}
                  className="group relative flex items-center gap-2.5 px-3 py-1.5 rounded-xl bg-slate-100 dark:bg-[#12121a] border border-slate-200 dark:border-[#222230] text-xs transition-all hover:border-orange-400 dark:hover:border-orange-500/40 shadow-xs max-w-xs"
                >
                  {/* File Format Icon (thumbnail for images) */}
                  {isImage && att.thumbUrl ? (
                    <img
                      src={att.thumbUrl}
                      alt=""
                      className="w-8 h-8 rounded-md object-cover border border-violet-500/30 shrink-0"
                    />
                  ) : (
                  <div className={`p-1 rounded-md border shrink-0 ${getFileBadgeColor(att.file_type)}`}>
                    {isLink ? (
                      <Link2 className="w-3.5 h-3.5" />
                    ) : isPdf ? (
                      <FileText className="w-3.5 h-3.5" />
                    ) : isDoc ? (
                      <FileText className="w-3.5 h-3.5" />
                    ) : isData ? (
                      <FileSpreadsheet className="w-3.5 h-3.5" />
                    ) : isImage ? (
                      <ImageIcon className="w-3.5 h-3.5" />
                    ) : (
                      <FileCode className="w-3.5 h-3.5" />
                    )}
                  </div>
                  )}

                  {/* Name and extraction status */}
                  <div className="flex flex-col min-w-0 pr-1">
                    <span className="font-semibold text-slate-800 dark:text-slate-100 truncate max-w-[170px]" title={att.name}>
                      {att.name}
                    </span>
                    <span className="text-[10px] text-slate-500 dark:text-slate-400 flex items-center gap-1">
                      {att.status === 'extracting' && (
                        <>
                          <Loader2 className="w-2.5 h-2.5 text-orange-500 animate-spin" />
                          <span>{isLink ? 'Reading link...' : isImage ? 'Reading image...' : 'Extracting content...'}</span>
                        </>
                      )}
                      {att.status === 'ready' && (
                        <>
                          <CheckCircle2 className="w-2.5 h-2.5 text-emerald-500" />
                          <span>
                            {isImage
                              ? `${att.analysis_method === 'ocr' ? 'Text read (OCR)' : 'Image analysed'} · `
                              : att.page_count ? `${att.page_count} ${att.page_count === 1 ? 'page' : 'pages'} · ` : ''}
                            {formatFileSize(att.size)}
                          </span>
                        </>
                      )}
                      {att.status === 'error' && (
                        <>
                          <AlertCircle className="w-2.5 h-2.5 text-rose-500" />
                          <span className="text-rose-500 truncate max-w-[130px]">{att.errorMessage || 'Failed'}</span>
                        </>
                      )}
                    </span>
                  </div>

                  {/* Remove attachment button */}
                  <button
                    type="button"
                    onClick={() => removeAttachment(att.id)}
                    className="p-1 rounded-full text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 hover:bg-slate-200 dark:hover:bg-slate-800 transition cursor-pointer"
                    title={isLink ? 'Remove link' : 'Remove file'}
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              );
            })}
          </div>
        )}

        {/* Text Input */}
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={
            isListening
              ? 'Listening... speak now'
              : attachments.length > 0
              ? 'Ask about your attached document, or press Enter for a summary…'
              : 'Ask Clyptus.AI about SAP BRIM: billing, invoicing, FI-CA or subscriptions…'
          }
          rows={1}
          disabled={isLoading}
          className="w-full bg-transparent text-slate-900 dark:text-slate-100 placeholder-slate-400 dark:placeholder-slate-500 text-sm px-4 pt-3.5 pb-12 focus:outline-none resize-none max-h-48 min-h-[52px]"
        />

        {/* Bottom Actions Bar */}
        <div className="absolute bottom-2.5 left-3 right-3 flex items-center justify-between pointer-events-auto">
          {/* Left side: ChatGPT-Style '+' Button & Mic Status */}
          <div className="relative flex items-center gap-2">
            {/* '+' Add Button */}
            <div className="relative">
              <button
                ref={attachButtonRef}
                type="button"
                onClick={() => setShowAttachMenu((prev) => !prev)}
                disabled={isLoading}
                title="Add files, folders or links"
                className={`flex items-center justify-center w-8 h-8 rounded-xl transition duration-150 disabled:opacity-40 disabled:cursor-not-allowed ${
                  showAttachMenu
                    ? 'bg-orange-500 text-white shadow-md shadow-orange-500/25 ring-2 ring-orange-500/30'
                    : 'bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white'
                }`}
              >
                <Plus className={`w-4 h-4 transition-transform duration-200 ${showAttachMenu ? 'rotate-45' : ''}`} />
              </button>

              {/* Attachment popup: a flat list, one icon and one label per row (like ChatGPT) */}
              {showAttachMenu && (
                <div
                  ref={menuRef}
                  role="menu"
                  className="absolute bottom-11 left-0 z-40 w-64 bg-white dark:bg-[#212129] border border-slate-200 dark:border-[#34343f] rounded-2xl shadow-xl p-1.5 animate-in fade-in zoom-in-95 duration-150"
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => { setShowAttachMenu(false); filesInputRef.current?.click(); }}
                    className="w-full flex items-center gap-3 px-3 py-2 rounded-xl text-left text-sm text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-[#1c1c28] transition-colors cursor-pointer"
                  >
                    <Paperclip className="w-[18px] h-[18px] shrink-0 text-slate-600 dark:text-slate-300" />
                    <span>Upload files</span>
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => { setShowAttachMenu(false); folderInputRef.current?.click(); }}
                    className="w-full flex items-center gap-3 px-3 py-2 rounded-xl text-left text-sm text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-[#1c1c28] transition-colors cursor-pointer"
                  >
                    <FolderOpen className="w-[18px] h-[18px] shrink-0 text-slate-600 dark:text-slate-300" />
                    <span>Upload a folder</span>
                  </button>
                  {showLinkInput ? (
                    <div className="flex items-center gap-2 px-3 py-1.5">
                      <Link2 className="w-[18px] h-[18px] shrink-0 text-slate-600 dark:text-slate-300" />
                      <input
                        type="url"
                        value={linkDraft}
                        autoFocus
                        onChange={(e) => setLinkDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') { e.preventDefault(); void submitLinkDraft(); }
                          if (e.key === 'Escape') { setShowLinkInput(false); setLinkDraft(''); }
                        }}
                        placeholder="Paste a link and press Enter"
                        aria-label="Link address"
                        className="min-w-0 flex-1 bg-transparent text-sm text-slate-900 dark:text-slate-100 placeholder-slate-400 dark:placeholder-slate-500 focus:outline-none"
                      />
                    </div>
                  ) : (
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => setShowLinkInput(true)}
                      className="w-full flex items-center gap-3 px-3 py-2 rounded-xl text-left text-sm text-slate-800 dark:text-slate-100 hover:bg-slate-100 dark:hover:bg-[#1c1c28] transition-colors cursor-pointer"
                    >
                      <Link2 className="w-[18px] h-[18px] shrink-0 text-slate-600 dark:text-slate-300" />
                      <span>Add a link</span>
                    </button>
                  )}
                </div>
              )}
            </div>

            <ModeToggle mode={mode} onModeChange={onModeChange} />

            {/* Listening status */}
            {isListening && (
              <span className="flex items-center gap-1.5 text-[11px] text-rose-500 font-medium animate-pulse">
                <span className="w-2 h-2 rounded-full bg-rose-500 inline-block animate-pulse" />
                Listening...
              </span>
            )}
          </div>

          {/* Right side: Voice + Send Buttons */}
          <div className="flex items-center gap-1.5">
            {/* Mic Button */}
            {voiceSupported && (
              <button
                type="button"
                onClick={toggleVoice}
                disabled={isLoading}
                title={isListening ? 'Stop recording' : 'Voice input'}
                className={`flex items-center justify-center w-8 h-8 rounded-xl transition duration-150 disabled:opacity-40 disabled:cursor-not-allowed ${
                  isListening
                    ? 'bg-rose-600 hover:bg-rose-500 text-white shadow-md shadow-rose-600/30 animate-pulse'
                    : 'bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200'
                }`}
              >
                {isListening ? <MicOff className="w-4 h-4" /> : <Mic className="w-4 h-4" />}
              </button>
            )}

            {/* Send / Stop Button */}
            {isLoading ? (
              <button
                type="button"
                onClick={onStop}
                title="Stop generating"
                className="flex items-center justify-center w-8 h-8 rounded-xl bg-rose-600 hover:bg-rose-500 text-white shadow-md shadow-rose-600/30 transition duration-150 cursor-pointer"
              >
                <Square className="w-3.5 h-3.5 fill-current" />
              </button>
            ) : (
              <button
                type="submit"
                disabled={(!input.trim() && !attachments.some((a) => a.status === 'ready' && !!a.text)) || isExtracting}
                title={isExtracting ? 'Reading your files and links...' : 'Send message'}
                className="flex items-center justify-center w-8 h-8 rounded-xl bg-gradient-to-r from-orange-600 to-orange-500 hover:from-orange-500 hover:to-orange-400 disabled:opacity-40 disabled:hover:from-orange-600 disabled:hover:to-orange-500 text-white shadow-md shadow-orange-500/30 transition duration-150 disabled:cursor-not-allowed cursor-pointer"
              >
                {isExtracting ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Send className="w-4 h-4" />
                )}
              </button>
            )}
          </div>
        </div>
      </form>

      <div className="text-center mt-2 flex items-center justify-center gap-1.5 flex-wrap">
        <span className="text-[11px] text-slate-400 dark:text-slate-500">
          Click <strong className="text-orange-500 font-semibold">+</strong> to add files, folders or links (or drop them here, or paste a link or screenshot). Clyptus.AI reads them and answers from them.
        </span>
      </div>
    </div>
  );
};
