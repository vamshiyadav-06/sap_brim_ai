import type { User, Chat, ChatMode, Message, MessageFeedback,Citation, WebSource, WebImage, DocumentAttachment, DocumentListResponse, ProcessingProgress, TokenSavingsSummary, TokenUsage } from '../types';
import type {
  ActivationCodeDetails,
  AdminOrganisation,
  AdminOrganisationDetail,
  AdminOrganisationIssued,
  AuthTokenResponse,
  IndividualUser,
  IndividualUsersResponse,
  IssuedActivationCode,
  KnowledgeCategory,
  KnowledgeListResponse,
  OrganisationStatus,
  OrgActivity,
  OrgMe,
  OrgMember,
  OrgMemberCreated,
  OrgRequestPayload,
  OrgRole,
  OrgSummary,
  SharedAnswerComment,
  SharedAnswerDetail,
} from '../types';
import { API_BASE_URL } from './apiBase';

export const TOKEN_STORAGE_KEY = 'sap_assistant_token';
const REFRESH_REQUEST_HEADER = 'X-Refresh-Request';

export const getStoredToken = (): string | null => {
  return localStorage.getItem(TOKEN_STORAGE_KEY);
};

export const setStoredToken = (token: string): void => {
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
};

export const clearStoredToken = (): void => {
  localStorage.removeItem(TOKEN_STORAGE_KEY);
};

const authHeaders = (): Record<string, string> => {
  const token = getStoredToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
};

const SERVER_UNREACHABLE = 'Cannot reach the server. Check your connection and make sure the backend is running.';
const SERVER_TIMEOUT = 'The server stopped responding. Please try again.';
const SESSION_EXPIRED = 'Your session has expired. Please sign in again.';
/** The single message the backend returns for every activation failure. */
export const ACTIVATION_CODE_INVALID = 'This activation code is not valid. Ask your Clyptus administrator for a new code.';
const CREATE_CHAT_TIMEOUT_MS = 15_000;

/** A failed request, with the HTTP status when the server answered. */
export class ApiError extends Error {
  status?: number;
  /** The parsed JSON error body, when the server sent one (for example `{detail, id}` on a 409). */
  body?: Record<string, unknown>;
  constructor(message: string, status?: number, body?: Record<string, unknown>) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

const httpError = (resp: Response, action: string): ApiError =>
  new ApiError(
    resp.status === 401
      ? SESSION_EXPIRED
      : resp.status === 402
        ? 'You have used all your tokens.'
        : resp.status === 429
          ? 'You already have answers in progress. Wait for one to finish, then try again.'
          : resp.status === 503
            ? 'The assistant is busy right now. Please try again in a few seconds.'
            : `${action} (HTTP ${resp.status}).`,
    resp.status,
  );

// A 401 on a request that carried our token means the session ended (expired, revoked or logged out
// elsewhere). One handler, registered by AuthContext, clears state and returns the user to sign-in.
let unauthorizedHandler: (() => void) | null = null;
export const setUnauthorizedHandler = (handler: (() => void) | null): void => {
  unauthorizedHandler = handler;
};

const carriesToken = (init?: RequestInit): boolean => {
  const headers = init?.headers;
  if (!headers) return false;
  if (headers instanceof Headers) return headers.has('Authorization');
  if (Array.isArray(headers)) return headers.some(([name]) => name.toLowerCase() === 'authorization');
  return Object.keys(headers).some((name) => name.toLowerCase() === 'authorization');
};

const reportIfUnauthorized = (resp: Response, init?: RequestInit): void => {
  if (resp.status === 401 && carriesToken(init)) unauthorizedHandler?.();
};

let refreshInFlight: Promise<string | null> | null = null;

/** Rotate the HttpOnly refresh cookie. Null means the backend definitively rejected the session. */
const refreshAccessToken = (): Promise<string | null> => {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
      method: 'POST',
      headers: { [REFRESH_REQUEST_HEADER]: '1' },
      credentials: 'include',
    });
    if (response.status === 401 || response.status === 403) return null;
    if (!response.ok) throw httpError(response, 'Could not refresh the session');
    const session = (await response.json()) as AuthTokenResponse;
    if (!session.access_token) throw new ApiError('The server returned an invalid session response.');
    setStoredToken(session.access_token);
    return session.access_token;
  })().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
};

const withBearer = (init: RequestInit | undefined, token: string): RequestInit => {
  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${token}`);
  return { ...init, headers, credentials: init?.credentials ?? 'same-origin' };
};

/** Retry one expired access token after a CSRF-protected refresh; refresh errors never sign the user out. */
const authenticatedFetch = async (input: string, init?: RequestInit): Promise<Response> => {
  const first = await fetch(input, init);
  if (first.status !== 401 || !carriesToken(init)) return first;

  const token = await refreshAccessToken();
  if (!token) {
    return first;
  }
  const retry = await fetch(input, withBearer(init, token));
  return retry;
};

/** fetch for authenticated API calls: refreshes once before reporting a rejected session. */
const authFetch = async (input: string, init?: RequestInit): Promise<Response> => {
  const resp = await authenticatedFetch(input, init);
  reportIfUnauthorized(resp, init);
  return resp;
};

export const apiLogout = async (): Promise<void> => {
  await authFetch(`${API_BASE_URL}/auth/logout`, { method: 'POST', headers: authHeaders() });
};

// fetch() rejects with a bare "Failed to fetch" when the backend is down; surface the real cause.
// A caller's own abort (Stop button) is passed through unchanged so it can be told apart.
const fetchBackend = async (url: string, init?: RequestInit): Promise<Response> => {
  try {
    const resp = await authenticatedFetch(url, init);
    reportIfUnauthorized(resp, init);
    return resp;
  } catch (err) {
    if (init?.signal?.aborted) {
      if ((init.signal.reason as Error | undefined)?.name === 'TimeoutError') throw new ApiError(SERVER_TIMEOUT);
      throw err;
    }
    throw new ApiError(SERVER_UNREACHABLE);
  }
};

// Auth API
/** POST /auth/register: creates the account and returns it; it does not start a session. */
export const apiRegister = async (name: string, email: string, password: string): Promise<User> => {
  const resp = await fetchBackend(`${API_BASE_URL}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, email, password }),
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Registration failed');
  }
  return resp.json();
};

export const apiLogin = async (email: string, password: string, unlockKey?: string) => {
  const resp = await fetchBackend(`${API_BASE_URL}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, ...(unlockKey ? { unlock_key: unlockKey } : {}) }),
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new ApiError(errorData.detail || 'Invalid email or password', resp.status);
  }
  return resp.json();
};

export const apiGetRegistrationStatus = async (): Promise<{ registration_enabled: boolean }> => {
  const resp = await fetchBackend(`${API_BASE_URL}/auth/registration-status`);
  if (!resp.ok) throw new ApiError('Could not load sign-up availability.', resp.status);
  return resp.json();
};

export const apiAdminLogin = async (email: string, password: string, unlockKey?: string) => {
  const resp = await fetchBackend(`${API_BASE_URL}/auth/admin-login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, ...(unlockKey ? { unlock_key: unlockKey } : {}) }),
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new ApiError(errorData.detail || 'Invalid administrator email or password', resp.status);
  }
  return resp.json();
};


export const apiGetMe = async (): Promise<User> => {
  const resp = await authFetch(`${API_BASE_URL}/auth/me`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    throw new ApiError('Unauthorized', resp.status);
  }
  return resp.json();
};

// Chats API
export const apiGetChats = async (search?: string, projectId?: string): Promise<Chat[]> => {
  // A plain string, not `new URL(...)`: the default API base "/api" is relative to the page.
  const url = `${API_BASE_URL}${withQuery('/chats', { search, project_id: projectId })}`;

  const resp = await authFetch(url, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) throw new Error('Failed to load chats');
  return resp.json();
};

/** Starts a chat; with `projectId` the chat is saved in that project (one of the user's own). */
export const apiCreateChat = async (title?: string, projectId?: string): Promise<Chat> => {
  const resp = await fetchBackend(`${API_BASE_URL}/chats`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(projectId ? { title: title || 'New SAP Chat', project_id: projectId } : { title: title || 'New SAP Chat' }),
    signal: AbortSignal.timeout(CREATE_CHAT_TIMEOUT_MS),
  });
  if (!resp.ok) throw httpError(resp, 'Could not start a new chat');
  return resp.json();
};

// ==========================================
// Projects (folders for related chats, stored on the server)
// ==========================================

export interface ServerProject {
  id: string;
  name: string;
  description: string | null;
  is_pinned: boolean;
  created_at: string;
  updated_at: string;
  chat_count: number;
}

const projectRequest = async <T>(path: string, action: string, init?: RequestInit): Promise<T> => {
  const resp = await authFetch(`${API_BASE_URL}/projects${path}`, {
    ...init,
    headers: { ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...authHeaders() },
  });
  if (!resp.ok) throw httpError(resp, action);
  return (resp.status === 204 ? undefined : await resp.json()) as T;
};

export const apiGetProjects = (): Promise<ServerProject[]> => projectRequest('', 'Could not load projects');

export const apiCreateProject = (project: { name: string; description?: string; is_pinned?: boolean }): Promise<ServerProject> =>
  projectRequest('', 'Could not create the project', { method: 'POST', body: JSON.stringify(project) });

export const apiUpdateProject = (
  id: string,
  changes: { name?: string; description?: string; is_pinned?: boolean },
): Promise<ServerProject> =>
  projectRequest(`/${encodeURIComponent(id)}`, 'Could not update the project', {
    method: 'PATCH',
    body: JSON.stringify(changes),
  });

/** Deletes the project only; its chats stay in the chat list. */
export const apiDeleteProject = (id: string): Promise<void> =>
  projectRequest(`/${encodeURIComponent(id)}`, 'Could not delete the project', { method: 'DELETE' });

export const apiGetChat = async (chatId: string): Promise<Chat & { messages: Message[] }> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) throw new Error('Chat not found');
  return resp.json();
};

export const apiUpdateChat = async (chatId: string, title: string): Promise<Chat> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) throw new Error('Failed to update chat');
  return resp.json();
};

export const apiDeleteChat = async (chatId: string): Promise<void> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}`, {
    method: 'DELETE',
    headers: { ...authHeaders() },
  });
  if (!resp.ok) throw new Error('Failed to delete chat');
};

export const apiSetQuestionLanguage = async (language: string | null): Promise<User> => {
  const resp = await authFetch(`${API_BASE_URL}/auth/me/question-language`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ language }),
  });
  if (!resp.ok) throw new Error('Failed to change the question language');
  return resp.json();
};

export const apiSetLanguage = async (language: string): Promise<User> => {
  const resp = await authFetch(`${API_BASE_URL}/auth/me/language`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ language }),
  });
  if (!resp.ok) throw new Error('Failed to change the language');
  return resp.json();
};

/** A thing the user asked the assistant to remember ("save it"). */
export interface SavedMemory {
  id: string;
  content: string;
  created_at: string | null;
}

export const apiListMemories = async (): Promise<SavedMemory[]> => {
  const resp = await authFetch(`${API_BASE_URL}/memories`, { headers: { ...authHeaders() } });
  if (!resp.ok) throw new Error('Failed to load saved memory');
  return resp.json();
};

export const apiDeleteMemory = async (id: string): Promise<void> => {
  const resp = await authFetch(`${API_BASE_URL}/memories/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { ...authHeaders() },
  });
  if (!resp.ok) throw new Error('Failed to delete saved memory');
};

export const apiDeleteAllMemories = async (): Promise<void> => {
  const resp = await authFetch(`${API_BASE_URL}/memories`, { method: 'DELETE', headers: { ...authHeaders() } });
  if (!resp.ok) throw new Error('Failed to delete saved memory');
};

// Messages API
/** GET /auth/me/token-savings: estimated tokens saved across the user's answers. */
export const apiGetTokenSavings = async (): Promise<TokenSavingsSummary> => {
  const resp = await authFetch(`${API_BASE_URL}/auth/me/token-savings`, { headers: { ...authHeaders() } });
  if (!resp.ok) throw new Error('Failed to load token savings');
  return resp.json();
};

export const apiGetMessages = async (chatId: string): Promise<Message[]> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}/messages`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) throw new Error('Failed to load messages');
  return resp.json();
};

export const apiExtractDocument = async (file: File): Promise<{
  filename: string;
  file_type: string;
  size_bytes: number;
  page_count: number;
  char_count: number;
  preview: string;
  text: string;
  detected_question?: string | null;
  analysis_method?: 'vision' | 'vision+ocr' | 'ocr' | null;
}> => {
  const formData = new FormData();
  formData.append('file', file);
  const headers = authHeaders();
  // Ensure browser sets multipart/form-data boundary automatically
  delete (headers as Record<string, string>)['Content-Type'];

  const resp = await authFetch(`${API_BASE_URL}/chats/extract-document`, {
    method: 'POST',
    headers,
    body: formData,
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.detail || `Failed to extract document (${resp.statusText})`);
  }
  return resp.json();
};

/** Downloads a document the assistant created for this user (fetched with the login, then saved by the browser). */
export const apiDownloadGeneratedFile = async (fileId: string, filename: string): Promise<void> => {
  const resp = await authFetch(`${API_BASE_URL}/files/${fileId}`, { headers: { ...authHeaders() } });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(typeof err.detail === 'string' ? err.detail : 'The file could not be downloaded.');
  }
  const url = URL.createObjectURL(await resp.blob());
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
};

/** Reads a web page or online file the user linked (the server only opens public http/https addresses). */
export const apiExtractUrl = async (url: string): Promise<{
  filename: string;
  file_type: string;
  size_bytes: number;
  page_count: number;
  char_count: number;
  preview: string;
  text: string;
}> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/extract-url`, {
    method: 'POST',
    headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(typeof err.detail === 'string' ? err.detail : 'That link could not be read.');
  }
  return resp.json();
};

export const apiSendMessage = async (
  chatId: string,
  content: string,
  attachments?: DocumentAttachment[]
): Promise<Message> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      content,
      attachments: attachments && attachments.length > 0 ? attachments : undefined
    }),
  });
  if (!resp.ok) throw new Error('Failed to send message');
  return resp.json();
};

/** Saves what was shown of a stopped answer; needs the stream id the server sent at stream start. */
export const apiSavePartialMessage = async (chatId: string, content: string, streamId: string): Promise<void> => {
  try {
    await authFetch(`${API_BASE_URL}/chats/${chatId}/messages/save-partial`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ content, stream_id: streamId }),
    });
  } catch (err) {
    console.error('Failed to save partial message:', err);
  }
};

/** Saves a thumbs up/down on an answer; null clears it. */
export const apiSetFeedback = async (chatId: string, messageId: string, value: MessageFeedback | null): Promise<void> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}/messages/${messageId}/feedback`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ value }),
  });
  if (!resp.ok) throw new Error('Could not save feedback');
};

/** Creates (or refreshes) the public link for one prompt or answer and returns its token. */
export const apiSharePrompt = async (chatId: string, messageId: string): Promise<string> => {
  const resp = await authFetch(`${API_BASE_URL}/chats/${chatId}/messages/${messageId}/share`, {
    method: 'POST',
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error('Could not create share link');
  return (await resp.json()).token;
};

/** Public: no login needed to read a shared prompt (and its answer, when an answer was shared). */
export const apiGetSharedPrompt = async (
  token: string
): Promise<{ content: string; answer: string | null; created_at: string }> => {
  const resp = await fetch(`${API_BASE_URL}/shared/prompts/${encodeURIComponent(token)}`);
  if (!resp.ok) throw new Error('Shared prompt not found');
  return resp.json();
};

// SSE Streaming
export interface StreamStatus {
  type: 'status';
  stage: 'thinking' | 'retrieval' | 'reranking' | 'grounding' | 'generation' | 'document_analysis' | string;
  message: string;
}

export interface StreamMetadata {
  message_id?: string;
  source_type?: 'knowledge_base' | 'github_repo' | 'web' | 'combined' | 'abstention' | 'insufficient' | 'refusal' | 'error' | 'interrupted' | 'attached_document' | 'unavailable';
  grounding_score?: number | null;
  citations?: Citation[];
  web_sources?: WebSource[];
  images?: WebImage[];
  is_in_rag_pipeline?: boolean;
  kb_score?: number;
  web_score?: number;
  winning_score?: number;
  source_scores?: Record<string, number>;
  comparison_audit?: Record<string, unknown>;
  verification_status?: 'verified' | 'partial' | 'unverified' | 'abstention' | 'unavailable';
  internal_evidence_confidence?: number;
  selected_evidence_quality?: number;
  external_search_used?: boolean;
  follow_up_questions?: string[];
  full_answer?: string;
  token_usage?: TokenUsage;
  /** The user's free tokens left after this answer. */
  token_balance?: number;
  llm_provider?: string;
  llm_model?: string;
  regeneration_count?: number;
}

export interface StreamCallbacks {
  onStatus?: (status: StreamStatus) => void;
  onMetadata?: (meta: StreamMetadata) => void;
  onToken?: (token: string) => void;
  /** Running token estimate for this answer; only ever grows. The final figures arrive in done's token_usage. */
  onUsage?: (tokens: number) => void;
  onDone?: (meta: StreamMetadata, fullAnswer?: string) => void;
  onError?: (err: any) => void;
  /** Stored id of the prompt that this stream answers. */
  onUserMessage?: (messageId: string) => void;
  onStreamId?: (streamId: string) => void;
}

export const apiStreamMessage = (
  chatId: string,
  content: string,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
  attachments?: DocumentAttachment[],
  mode: ChatMode = 'scholar'
): Promise<void> =>
  streamSSE(
    `${API_BASE_URL}/chats/${chatId}/messages?stream=true`,
    { content, attachments: attachments && attachments.length > 0 ? attachments : undefined, mode },
    callbacks,
    signal
  );

/** Replaces a stored prompt, drops every later message, and streams the new answer. */
export const apiEditMessageStream = (
  chatId: string,
  messageId: string,
  content: string,
  callbacks: StreamCallbacks,
  signal?: AbortSignal,
  mode: ChatMode = 'scholar'
): Promise<void> =>
  streamSSE(`${API_BASE_URL}/chats/${chatId}/messages/${messageId}/edit`, { content, mode }, callbacks, signal);

// The backend sends the response headers at once and then an event at every pipeline stage, so this
// much silence means the connection is stuck (e.g. a hung dev server), not that the answer is slow.
const STREAM_CONNECT_TIMEOUT_MS = 30_000;
const STREAM_IDLE_TIMEOUT_MS = 120_000;

/**
 * Posts a question and reads the SSE answer. Always ends in exactly one of onDone or onError,
 * except when the caller aborts (Stop), which ends silently.
 */
export const streamSSE = async (
  url: string,
  body: Record<string, unknown>,
  callbacks: StreamCallbacks,
  signal?: AbortSignal
): Promise<void> => {
  // Aborted by the caller's signal or by the watchdog when the server goes quiet.
  const controller = new AbortController();
  const forwardAbort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener('abort', forwardAbort);

  let timedOut = false;
  let watchdog: ReturnType<typeof setTimeout> | undefined;
  const expectDataWithin = (ms: number) => {
    clearTimeout(watchdog);
    watchdog = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, ms);
  };

  let isCompleted = false;

  try {
    expectDataWithin(STREAM_CONNECT_TIMEOUT_MS);
    const resp = await fetchBackend(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    if (!resp.ok) {
      throw httpError(resp, 'The server could not answer this question');
    }

    const reader = resp.body?.getReader();
    if (!reader) throw new Error('ReadableStream not supported');

    const decoder = new TextDecoder();
    let buffer = '';
    let finalReceivedAnswer: string | undefined = undefined;
    let lastDoneMeta: StreamMetadata = {};

    const finishStream = (meta?: StreamMetadata, fullAnswer?: string) => {
      if (!isCompleted) {
        isCompleted = true;
        const effectiveMeta = meta || lastDoneMeta;
        callbacks.onDone?.(effectiveMeta, fullAnswer || finalReceivedAnswer);
      }
    };

    const processBlock = (block: string) => {
      const trimmed = block.trim();
      if (!trimmed) return;

      const lines = trimmed.split('\n');
      for (const line of lines) {
        if (isCompleted) return;
        const trimmedLine = line.trim();
        if (trimmedLine.startsWith('data:')) {
          const rawData = trimmedLine.slice(5).trim();
          if (!rawData) continue;

          try {
            const parsed = JSON.parse(rawData);

            if (parsed.type === 'status') {
              callbacks.onStatus?.(parsed as StreamStatus);
            } else if (parsed.type === 'token') {
              const tokenContent = parsed.content ?? parsed.token ?? '';
              if (tokenContent) {
                callbacks.onToken?.(tokenContent);
              }
            } else if (parsed.type === 'done') {
              lastDoneMeta = parsed;
              const ans = parsed.answer || parsed.full_answer;
              if (ans) {
                finalReceivedAnswer = ans;
              }
              finishStream(parsed, ans);
            } else if (parsed.type === 'error') {
              // The backend ends the stream after an error event; nothing after it is an answer.
              isCompleted = true;
              callbacks.onError?.(new Error(parsed.message || 'Generation error occurred'));
            } else if (parsed.type === 'usage') {
              if (typeof parsed.tokens === 'number') callbacks.onUsage?.(parsed.tokens);
            } else if (parsed.type === 'user_message') {
              callbacks.onUserMessage?.(parsed.message_id);
            } else if (parsed.type === 'stream') {
              callbacks.onStreamId?.(parsed.stream_id);
            } else if (parsed.citations !== undefined || parsed.message_id !== undefined) {
              lastDoneMeta = { ...lastDoneMeta, ...parsed };
              callbacks.onMetadata?.(parsed);
            }
          } catch (e) {
            console.error('SSE JSON parse error:', e, rawData);
          }
        }
      }
    };

    while (true) {
      expectDataWithin(STREAM_IDLE_TIMEOUT_MS);
      const { done, value } = await reader.read();
      if (done) {
        if (buffer.trim()) {
          processBlock(buffer);
          buffer = '';
        }
        break;
      }

      buffer += decoder.decode(value, { stream: true });
      const normalized = buffer.replace(/\r\n/g, '\n');
      const blocks = normalized.split('\n\n');
      buffer = blocks.pop() || '';

      for (const block of blocks) {
        processBlock(block);
      }
    }

    // The backend always ends with a done or error event; closing without one means it was cut off.
    if (!isCompleted) {
      isCompleted = true;
      callbacks.onError?.(new Error('The answer was cut off before it finished. Please try again.'));
    }
  } catch (err: any) {
    if (isCompleted) return;
    isCompleted = true;
    if (timedOut) {
      callbacks.onError?.(new ApiError(SERVER_TIMEOUT));
      return;
    }
    if (signal?.aborted || err?.name === 'AbortError') {
      console.log('Streaming aborted by user.');
      return;
    }
    callbacks.onError?.(err);
  } finally {
    clearTimeout(watchdog);
    signal?.removeEventListener('abort', forwardAbort);
  }
};

// ==========================================
// Document Management & Processing API
// ==========================================

export const apiGetDocuments = async (params?: {
  status?: string;
  search?: string;
  page?: number;
  page_size?: number;
}): Promise<DocumentListResponse> => {
  const url = `${API_BASE_URL}${withQuery('/documents', {
    status: params?.status !== 'all' ? params?.status : undefined,
    search: params?.search,
    page: params?.page || undefined,
    page_size: params?.page_size || undefined,
  })}`;

  const resp = await authFetch(url, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to fetch knowledge base documents');
  }
  return resp.json();
};

export const apiGetDocumentDetails = async (documentId: string): Promise<any> => {
  const resp = await authFetch(`${API_BASE_URL}/documents/${documentId}`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    throw new Error('Failed to fetch document details');
  }
  return resp.json();
};

export const apiUploadDocuments = async (files: File[]): Promise<{
  status: string;
  registered_count: number;
  documents: any[];
  errors: any[];
}> => {
  const formData = new FormData();
  files.forEach((file) => {
    formData.append('files', file);
  });

  const resp = await authFetch(`${API_BASE_URL}/documents/upload`, {
    method: 'POST',
    headers: { ...authHeaders() },
    body: formData,
  });

  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to upload documents');
  }
  return resp.json();
};

export const apiProcessAllDocuments = async (force: boolean = false): Promise<{
  status: string;
  message: string;
}> => {
  const resp = await authFetch(`${API_BASE_URL}/documents/process-all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ force }),
  });

  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to trigger batch document processing');
  }
  return resp.json();
};

export const apiProcessIndividualDocument = async (
  documentId: string,
  force: boolean = true
): Promise<{ status: string; message: string }> => {
  const resp = await authFetch(`${API_BASE_URL}/documents/${documentId}/process`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ force }),
  });

  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to process document');
  }
  return resp.json();
};

export const apiGetProcessingStatus = async (): Promise<ProcessingProgress> => {
  const resp = await authFetch(`${API_BASE_URL}/documents/processing-status`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    throw new Error('Failed to fetch processing status');
  }
  return resp.json();
};

export const apiDeleteDocument = async (documentId: string): Promise<{
  success: boolean;
  deleted_document_id: string;
  deleted_filename: string;
  deleted_chunks: number;
  file_deleted_from_disk: boolean;
  vector_database_status: string;
}> => {
  const resp = await authFetch(`${API_BASE_URL}/documents/${documentId}`, {
    method: 'DELETE',
    headers: { ...authHeaders() },
  });

  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to delete document');
  }
  return resp.json();
};

export const connectProcessingStream = (
  onProgress: (progress: ProcessingProgress) => void,
  onError?: (error: any) => void
): (() => void) => {
  const controller = new AbortController();
  void (async () => {
    try {
      const response = await authFetch(`${API_BASE_URL}/documents/processing-stream`, {
        headers: authHeaders(), signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`Stream request failed (${response.status})`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split(/\r?\n\r?\n/);
        buffer = frames.pop() || '';
        for (const frame of frames) {
          const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim()).join('\n');
          if (data) onProgress(JSON.parse(data));
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) onError?.(error);
    }
  })();
  return () => controller.abort();
};

export interface SystemConfig {
  embedding_model: string;
  embedding_dim: number;
  llm_provider: string;
  llm_model: string;
  similarity_threshold: number;
  internal_evidence_threshold: number;
  grounding_threshold: number;
  admin_api_key_enabled: boolean;
  signed_in_admin: string;
}

export interface AdminSecurityControls {
  registration_enabled: boolean;
  login_max_failures: number;
  unlock_key_ttl_minutes: number;
}

export interface LoginLockout {
  id: string;
  name: string;
  email: string;
  account_type: string;
  locked_at: string | null;
  key_expires_at: string | null;
}

const adminSecurityRequest = async <T>(path: string, init?: RequestInit): Promise<T> => {
  const resp = await authFetch(`${API_BASE_URL}/admin${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...init?.headers },
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new ApiError(body.detail || 'Could not update authentication controls.', resp.status);
  return body as T;
};

export const apiAdminGetSecurityControls = (): Promise<AdminSecurityControls> =>
  adminSecurityRequest('/security-controls');

export const apiAdminSetRegistration = (registration_enabled: boolean): Promise<{ registration_enabled: boolean }> =>
  adminSecurityRequest('/security-controls/registration', {
    method: 'PUT', body: JSON.stringify({ registration_enabled }),
  });

export const apiAdminListLoginLockouts = (): Promise<{ users: LoginLockout[] }> =>
  adminSecurityRequest('/login-lockouts');

export const apiAdminIssueLoginUnlockKey = (userId: string): Promise<{ user_id: string; email: string; key: string; expires_at: string }> =>
  adminSecurityRequest(`/users/${encodeURIComponent(userId)}/login-unlock-key`, { method: 'POST' });

/** Admin only: the live model and threshold settings shown on the System tab. */
export const apiGetSystemConfig = async (): Promise<SystemConfig> => {
  const resp = await authFetch(`${API_BASE_URL}/rag-inspect/system-config`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to load system configuration');
  }
  return resp.json();
};

export const apiGetRAGInspectionDocuments = async (): Promise<any> => {
  const resp = await authFetch(`${API_BASE_URL}/rag-inspect/documents`, {
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to fetch knowledge base inspection status');
  }
  return resp.json();
};

export const apiTestSimilaritySearch = async (
  query: string,
  top_k: number = 10,
  include_web_search: boolean = false
): Promise<any> => {
  const resp = await authFetch(`${API_BASE_URL}/rag-inspect/similarity-search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ query, top_k, include_web_search }),
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to run similarity search inspection');
  }
  return resp.json();
};

export const apiTriggerIngest = async (): Promise<any> => {
  const resp = await authFetch(`${API_BASE_URL}/admin/documents/ingest`, {
    method: 'POST',
    headers: { ...authHeaders() },
  });
  if (!resp.ok) {
    const errorData = await resp.json().catch(() => ({}));
    throw new Error(errorData.detail || 'Failed to trigger document ingestion');
  }
  return resp.json();
};




// ==========================================
// Organisation Portal API (docs/ORGANISATION_PORTAL.md)
// ==========================================

/** Builds an ApiError from a failed response, surfacing FastAPI's string `detail` when present. */
const errorFromResponse = async (resp: Response, fallback: string, sessionEnds: boolean): Promise<ApiError> => {
  const body = (await resp.json().catch(() => null)) as Record<string, unknown> | null;
  const detail = typeof body?.detail === 'string' && body.detail.trim() ? body.detail : null;
  const message = resp.status === 401 && sessionEnds ? SESSION_EXPIRED : detail ?? fallback;
  return new ApiError(message, resp.status, body ?? undefined);
};

interface OrgRequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  /** Send the stored token. Public organisation endpoints leave it off. */
  auth?: boolean;
}

/** JSON request helper for the organisation endpoints. Resolves to undefined for 204 responses. */
const orgRequest = async <T>(path: string, fallback: string, options: OrgRequestOptions = {}): Promise<T> => {
  const { method = 'GET', body, auth = true } = options;
  const headers: Record<string, string> = { ...(auth ? authHeaders() : {}) };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const resp = await fetchBackend(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!resp.ok) throw await errorFromResponse(resp, fallback, auth);
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
};

const withQuery = (path: string, params: Record<string, string | number | undefined | null>): string => {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const qs = query.toString();
  return qs ? `${path}?${qs}` : path;
};

// --- Public organisation endpoints ---

/** POST /org/requests: the server answers the same way for every request, also for duplicates. */
export const apiOrgSubmitRequest = (payload: OrgRequestPayload): Promise<{ status: string }> =>
  orgRequest('/org/requests', 'We could not send your request. Please try again.', {
    method: 'POST',
    body: payload,
    auth: false,
  });

/** POST /org/activation/verify */
export const apiOrgVerifyActivation = (code: string): Promise<ActivationCodeDetails> =>
  orgRequest('/org/activation/verify', ACTIVATION_CODE_INVALID, { method: 'POST', body: { code }, auth: false });

/** POST /org/activation/complete: creates the owner account and returns its session. */
export const apiOrgCompleteActivation = (code: string, name: string, password: string): Promise<AuthTokenResponse> =>
  orgRequest('/org/activation/complete', ACTIVATION_CODE_INVALID, {
    method: 'POST',
    body: { code, name, password },
    auth: false,
  });

/** POST /org/auth/login: organisation accounts only. */
export const apiOrgLogin = (email: string, password: string, unlockKey?: string): Promise<AuthTokenResponse> =>
  orgRequest('/org/auth/login', 'Invalid email or password', {
    method: 'POST', body: { email, password, ...(unlockKey ? { unlock_key: unlockKey } : {}) }, auth: false,
  });

/** POST /auth/change-password: returns a new session; older tokens are revoked. */
export const apiChangePassword = (currentPassword: string, newPassword: string): Promise<AuthTokenResponse> =>
  orgRequest('/auth/change-password', 'Could not change your password.', {
    method: 'POST',
    body: { current_password: currentPassword, new_password: newPassword },
  });

// --- Organisation member endpoints ---

export const apiOrgGetMe = (): Promise<OrgMe> => orgRequest('/org/me', 'Could not load your organisation.');

export const apiOrgGetCategories = (): Promise<KnowledgeCategory[]> =>
  orgRequest('/org/knowledge/categories', 'Could not load knowledge topics.');

export const apiOrgListKnowledge = (
  params: { category?: string; q?: string; limit?: number; offset?: number } = {},
): Promise<KnowledgeListResponse> => orgRequest(withQuery('/org/knowledge', params), 'Could not load shared answers.');

/** POST /org/knowledge. "Already shared" comes back as an ApiError with status 409. */
export const apiOrgShareAnswer = (messageId: string): Promise<SharedAnswerDetail> =>
  orgRequest('/org/knowledge', 'Could not share this answer.', { method: 'POST', body: { message_id: messageId } });

export const apiOrgGetAnswer = (id: string): Promise<SharedAnswerDetail> =>
  orgRequest(`/org/knowledge/${encodeURIComponent(id)}`, 'Could not load this answer.');

export const apiOrgUpdateAnswerCategory = (id: string, category: string): Promise<SharedAnswerDetail> =>
  orgRequest(`/org/knowledge/${encodeURIComponent(id)}`, 'Could not change the topic.', {
    method: 'PATCH',
    body: { category },
  });

export const apiOrgDeleteAnswer = (id: string): Promise<void> =>
  orgRequest(`/org/knowledge/${encodeURIComponent(id)}`, 'Could not remove this answer.', { method: 'DELETE' });

/** POST (like) or DELETE (unlike) /org/knowledge/{id}/like; both are idempotent. */
export const apiOrgSetLike = (id: string, liked: boolean): Promise<{ liked: boolean; like_count: number }> =>
  orgRequest(`/org/knowledge/${encodeURIComponent(id)}/like`, 'Could not update your like.', {
    method: liked ? 'POST' : 'DELETE',
  });

export const apiOrgAddComment = (id: string, body: string): Promise<SharedAnswerComment> =>
  orgRequest(`/org/knowledge/${encodeURIComponent(id)}/comments`, 'Could not post your comment.', {
    method: 'POST',
    body: { body },
  });

export const apiOrgDeleteComment = (id: string, commentId: string): Promise<void> =>
  orgRequest(
    `/org/knowledge/${encodeURIComponent(id)}/comments/${encodeURIComponent(commentId)}`,
    'Could not delete the comment.',
    { method: 'DELETE' },
  );

export const apiOrgGetSharedMessageIds = (): Promise<{ message_ids: string[] }> =>
  orgRequest('/org/knowledge/shared-message-ids', 'Could not load shared answers.');

// --- Organisation admin endpoints (org_role owner or admin) ---

export const apiOrgAdminListMembers = (): Promise<OrgMember[]> =>
  orgRequest('/org/admin/members', 'Could not load members.');

/** The admin chooses the e-mail and starting password; the server never sends the password back. */
export const apiOrgAdminCreateMember = (member: {
  name: string;
  email: string;
  password: string;
  org_role: Exclude<OrgRole, 'owner'>;
}): Promise<OrgMemberCreated> =>
  orgRequest('/org/admin/members', 'Could not add the member.', { method: 'POST', body: member });

export const apiOrgAdminUpdateMember = (
  id: string,
  changes: { org_role?: Exclude<OrgRole, 'owner'>; is_active?: boolean },
): Promise<OrgMember | undefined> =>
  orgRequest(`/org/admin/members/${encodeURIComponent(id)}`, 'Could not update the member.', {
    method: 'PATCH',
    body: changes,
  });

/** Sets a password the admin chose; returns the updated member (the password is never sent back). */
export const apiOrgAdminResetPassword = (id: string, password: string): Promise<OrgMember> =>
  orgRequest(`/org/admin/members/${encodeURIComponent(id)}/reset-password`, 'Could not reset the password.', {
    method: 'POST',
    body: { password },
  });

export const apiOrgAdminGetActivity = (): Promise<OrgActivity> =>
  orgRequest('/org/admin/activity', 'Could not load activity.');

export const apiOrgAdminUpdateOrganisation = (changes: {
  name?: string;
  contact_email?: string;
}): Promise<Partial<OrgSummary> | undefined> =>
  orgRequest('/org/admin/organisation', 'Could not save the organisation settings.', {
    method: 'PATCH',
    body: changes,
  });

// --- Super Admin: organisations and individual users ---

export const apiAdminListOrganisations = (status?: OrganisationStatus | ''): Promise<AdminOrganisation[]> =>
  orgRequest(withQuery('/admin/organisations', { status }), 'Could not load organisations.');

export const apiAdminCreateOrganisation = (payload: {
  name: string;
  contact_email: string;
  contact_name?: string;
  admin_name: string;
  admin_email: string;
}): Promise<AdminOrganisationIssued> =>
  orgRequest('/admin/organisations', 'Could not create the organisation.', { method: 'POST', body: payload });

export const apiAdminApproveOrganisation = (
  id: string,
  admin: { admin_name: string; admin_email: string },
): Promise<AdminOrganisationIssued> =>
  orgRequest(`/admin/organisations/${encodeURIComponent(id)}/approve`, 'Could not approve the request.', {
    method: 'POST',
    body: admin,
  });

export const apiAdminRejectOrganisation = (id: string): Promise<unknown> =>
  orgRequest(`/admin/organisations/${encodeURIComponent(id)}/reject`, 'Could not reject the request.', {
    method: 'POST',
  });

export const apiAdminReissueActivationCode = (id: string): Promise<IssuedActivationCode> =>
  orgRequest(`/admin/organisations/${encodeURIComponent(id)}/activation-codes`, 'Could not issue a new code.', {
    method: 'POST',
  });

export const apiAdminSetOrganisationStatus = (
  id: string,
  status: Extract<OrganisationStatus, 'active' | 'suspended' | 'deactivated'>,
): Promise<unknown> =>
  orgRequest(`/admin/organisations/${encodeURIComponent(id)}/status`, 'Could not change the status.', {
    method: 'PATCH',
    body: { status },
  });

export const apiAdminGetOrganisation = (id: string): Promise<AdminOrganisationDetail> =>
  orgRequest(`/admin/organisations/${encodeURIComponent(id)}`, 'Could not load the organisation.');

export const apiAdminListIndividualUsers = (
  params: { search?: string; limit?: number; offset?: number } = {},
): Promise<IndividualUsersResponse> => orgRequest(withQuery('/admin/individual-users', params), 'Could not load users.');

export const apiAdminCreateIndividualUser = (body: {
  name: string;
  email: string;
  password: string;
}): Promise<IndividualUser> =>
  orgRequest('/admin/individual-users', 'Could not create the user.', { method: 'POST', body });

export const apiAdminSetUserStatus = (id: string, isActive: boolean): Promise<IndividualUser | undefined> =>
  orgRequest(`/admin/users/${encodeURIComponent(id)}/status`, 'Could not change the account status.', {
    method: 'PATCH',
    body: { is_active: isActive },
  });
