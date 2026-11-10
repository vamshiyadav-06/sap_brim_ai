import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiCreateChat, apiGetChats, apiGetMe, apiStreamMessage, getStoredToken, setUnauthorizedHandler, streamSSE, type StreamCallbacks } from './api';

const sse = (...events: object[]) => events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
const expiredSessionFetch = () => vi.fn()
  .mockResolvedValueOnce(new Response('{}', { status: 401 }))
  .mockResolvedValueOnce(new Response('{}', { status: 401 }));

/** A response whose body delivers `chunks` and then closes. */
const streamResponse = (chunks: string[], status = 200) => {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { 'Content-Type': 'text/event-stream' } });
};

/** A fetch that never answers but rejects like the real one when its signal aborts. */
const hangingFetch = (_url: string, init?: RequestInit) =>
  new Promise<Response>((_resolve, reject) => {
    init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
  });

const recorder = () => {
  const calls = { done: [] as unknown[][], error: [] as Error[], tokens: [] as string[] };
  const callbacks: StreamCallbacks = {
    onDone: (...args) => calls.done.push(args),
    onError: (err) => calls.error.push(err),
    onToken: (t) => calls.tokens.push(t),
  };
  return { calls, callbacks };
};

beforeEach(() => {
  const store = new Map<string, string>([['sap_assistant_token', 'test-token']]);
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('streamSSE', () => {
  it('sends the selected mode with a new streamed message', async () => {
    let requestBody: Record<string, unknown> | undefined;
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      requestBody = JSON.parse(String(init?.body));
      return streamResponse([sse({ type: 'done', answer: 'Direct SAP response' })]);
    }));

    await apiStreamMessage('chat-1', 'Explain SAP BRIM', {}, undefined, undefined, 'sprint');

    expect(requestBody).toEqual({ content: 'Explain SAP BRIM', mode: 'sprint' });
  });

  it.each([
    [503, /busy right now.*try again/i],
    [429, /answers in progress.*try again/i],
  ])('explains a %i from the capacity limit instead of a bare HTTP code', async (status, message) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"x"}', { status })));
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.error).toHaveLength(1);
    expect(calls.error[0].message).toMatch(message);
    expect((calls.error[0] as ApiError).status).toBe(status);
    expect(calls.done).toHaveLength(0);
  });

  it('hands the server-issued stream id to the caller (needed to save a stopped answer)', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse([
      sse({ type: 'stream', stream_id: 'signed-stream-id' }, { type: 'token', content: 'Hi' }),
      sse({ type: 'done', answer: 'Hi', message_id: 'a1' }),
    ])));
    const { calls, callbacks } = recorder();
    const ids: string[] = [];

    await streamSSE('/x', {}, { ...callbacks, onStreamId: (id) => ids.push(id) });

    expect(ids).toEqual(['signed-stream-id']);
    expect(calls.tokens.join('')).toBe('Hi');
  });

  it('delivers tokens and finishes once on a normal answer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse([
      sse({ type: 'user_message', message_id: 'm1' }, { type: 'status', stage: 'thinking', message: 'Thinking...' }),
      sse({ type: 'token', content: 'Hello ' }, { type: 'token', content: 'world' }),
      sse({ type: 'done', answer: 'Hello world', message_id: 'a1' }),
    ])));
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.tokens.join('')).toBe('Hello world');
    expect(calls.done).toHaveLength(1);
    expect(calls.done[0][1]).toBe('Hello world');
    expect(calls.error).toHaveLength(0);
  });

  it('reports the running token count and hands the final token usage to onDone', async () => {
    const usage = { used: 900, baseline: 4000, saved: 3100 };
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse([
      sse({ type: 'usage', tokens: 120 }, { type: 'token', content: 'Hello ' }),
      sse({ type: 'usage', tokens: 900 }, { type: 'usage', tokens: 'not-a-number' }, { type: 'token', content: 'world' }),
      sse({ type: 'done', answer: 'Hello world', message_id: 'a1', token_usage: usage }),
    ])));
    const { calls, callbacks } = recorder();
    const counts: number[] = [];

    await streamSSE('/x', {}, { ...callbacks, onUsage: (tokens) => counts.push(tokens) });

    expect(counts).toEqual([120, 900]);
    expect(calls.tokens.join('')).toBe('Hello world');
    expect((calls.done[0][0] as { token_usage?: unknown }).token_usage).toEqual(usage);
  });

  it('reports a backend error event once and never finishes as an answer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse([
      sse({ type: 'token', content: 'Part' }, { type: 'error', message: 'Generation interrupted. Please try again.' }),
    ])));
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.error.map((e) => e.message)).toEqual(['Generation interrupted. Please try again.']);
    expect(calls.done).toHaveLength(0);
  });

  it('reports a stream that closes without a done event', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse([sse({ type: 'token', content: 'Half an ans' })])));
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.done).toHaveLength(0);
    expect(calls.error).toHaveLength(1);
    expect(calls.error[0].message).toMatch(/cut off/);
  });

  it('survives malformed SSE lines', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => streamResponse([
      'data: {not json\n\n',
      sse({ type: 'done', answer: 'ok' }),
    ])));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.done).toHaveLength(1);
    expect(calls.error).toHaveLength(0);
  });

  it('says the session expired on HTTP 401', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"detail":"Could not validate credentials"}', { status: 401 })));
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.error).toHaveLength(1);
    expect(calls.error[0]).toBeInstanceOf(ApiError);
    expect((calls.error[0] as ApiError).status).toBe(401);
    expect(calls.error[0].message).toMatch(/session has expired/);
  });

  it('says the server is unreachable when fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
    const { calls, callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);

    expect(calls.error).toHaveLength(1);
    expect(calls.error[0].message).toMatch(/Cannot reach the server/);
  });

  it('gives up on a server that never responds', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn(hangingFetch));
    const { calls, callbacks } = recorder();

    const run = streamSSE('/x', {}, callbacks);
    await vi.advanceTimersByTimeAsync(30_000);
    await run;

    expect(calls.error).toHaveLength(1);
    expect(calls.error[0].message).toMatch(/stopped responding/);
    expect(calls.done).toHaveLength(0);
  });

  it('ends silently when the user stops generation', async () => {
    vi.stubGlobal('fetch', vi.fn(hangingFetch));
    vi.spyOn(console, 'log').mockImplementation(() => {});
    const { calls, callbacks } = recorder();
    const controller = new AbortController();

    const run = streamSSE('/x', {}, callbacks, controller.signal);
    controller.abort();
    await run;

    expect(calls.error).toHaveLength(0);
    expect(calls.done).toHaveLength(0);
  });
});

describe('apiCreateChat', () => {
  it('throws a session-expired ApiError on HTTP 401', async () => {
    vi.stubGlobal('fetch', expiredSessionFetch());

    const err = await apiCreateChat('q').catch((e) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(err.message).toMatch(/session has expired/);
  });

  it('throws a readable error when the backend is down', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));

    await expect(apiCreateChat('q')).rejects.toThrow(/Cannot reach the server/);
  });
});

describe('expired sessions', () => {
  afterEach(() => setUnauthorizedHandler(null));

  it('a 401 on an authenticated request calls the global handler once per request', async () => {
    vi.stubGlobal('fetch', expiredSessionFetch());
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    await expect(apiGetChats()).rejects.toBeTruthy();
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('a 401 on a request without a token (e.g. a wrong password) does not', async () => {
    vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 401 })));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    await expect(apiGetChats()).rejects.toBeTruthy();
    expect(handler).not.toHaveBeenCalled();
  });

  it('a 401 on the answer stream reports the expired session too', async () => {
    vi.stubGlobal('fetch', expiredSessionFetch());
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    const { callbacks } = recorder();

    await streamSSE('/x', {}, callbacks);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('refreshes an expired access token with the HttpOnly cookie and retries once', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: 'rotated-token', user: { id: 'u1' } }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'u1', email: 'u@example.com', name: 'User' }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    const user = await apiGetMe();

    expect(user.id).toBe('u1');
    expect(getStoredToken()).toBe('rotated-token');
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const refreshInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(refreshInit.method).toBe('POST');
    expect(refreshInit.credentials).toBe('include');
    expect(new Headers(refreshInit.headers).get('X-Refresh-Request')).toBe('1');
    const retryInit = fetchMock.mock.calls[2][1] as RequestInit;
    expect(new Headers(retryInit.headers).get('Authorization')).toBe('Bearer rotated-token');
  });

  it('reports an invalid refresh session as unauthorized', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockResolvedValueOnce(new Response('{}', { status: 401 })));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    await expect(apiGetMe()).rejects.toMatchObject({ status: 401 });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('keeps the access token when the refresh server is temporarily unavailable', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch')));
    const handler = vi.fn();
    setUnauthorizedHandler(handler);

    await expect(apiGetMe()).rejects.toThrow();
    expect(getStoredToken()).toBe('test-token');
    expect(handler).not.toHaveBeenCalled();
  });

  it('does not clear the token when the refresh endpoint returns a server error', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockResolvedValueOnce(new Response('{}', { status: 503 })));

    await expect(apiGetMe()).rejects.toMatchObject({ status: 503 });
    expect(getStoredToken()).toBe('test-token');
  });
});
