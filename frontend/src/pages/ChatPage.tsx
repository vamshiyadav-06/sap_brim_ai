import React, { useState, useEffect, useRef, useCallback } from 'react';
import { ChevronDown } from 'lucide-react';
import { useParams, useNavigate, useLocation, useSearchParams } from 'react-router-dom';
import { DEFAULT_CHAT_MODE, type ChatMode, type Message, type DocumentAttachment, type TokenUsage } from '../types';
import { ChatMessage } from '../components/chat/ChatMessage';
import { ChatSavingsLine } from '../components/chat/ChatSavingsLine';
import { recallTokenWallet, rememberTokenWallet, type TokenWallet } from '../components/chat/TokenBalance';
import { isStoredMessage } from '../components/chat/messageUtils';
import { ChatInput } from '../components/chat/ChatInput';
import { WelcomeScreen } from '../components/chat/WelcomeScreen';
import { TokenSavedToast } from '../components/chat/TokenSavedToast';
import { revealStep } from '../components/chat/reveal';
import { useAuth } from '../context/AuthContext';
import { useOrgSharedMessageIds } from '../hooks/useOrgSharedMessageIds';
import { isOrgUser } from '../layouts/accountRouting';
import { answersInProgress } from '../services/answersInProgress';
import { projectService } from '../services/projectService';

import {
  apiCreateChat,
  apiGetMessages,
  apiStreamMessage,
  apiEditMessageStream,
  apiSavePartialMessage,
  type StreamCallbacks,
  type StreamMetadata,
  type StreamStatus,
} from '../services/api';


/** An answer that failed while its chat was not on screen, shown when the chat is opened again. */
const failureMessage = (chatId: string, text: string): Message => ({
  id: `err-${chatId}-${Date.now()}`,
  chat_id: chatId,
  role: 'assistant',
  content: text,
  source_type: 'error',
  created_at: new Date().toISOString(),
  citations: [],
  web_sources: [],
});

export const ChatPage: React.FC = () => {
  const { conversationId } = useParams<{ conversationId?: string }>();
  const navigate = useNavigate();
  const { user } = useAuth();
  // Organisation users: which answers are already in the knowledge hub (fetched once per page).
  const { sharedIds: orgSharedIds, markShared: markOrgShared } = useOrgSharedMessageIds(isOrgUser(user));
  const location = useLocation();
  // A prompt opened from a share link pre-fills the input; the user decides whether to send it.
  const [prefillPrompt] = useState<string>(() => (location.state as { prefillPrompt?: string } | null)?.prefillPrompt ?? '');

  useEffect(() => {
    // Drop the router state so a page refresh doesn't pre-fill again.
    if (prefillPrompt) navigate(location.pathname, { replace: true, state: null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // /app?project=<id> (from a project page): the first question starts a chat saved in that project.
  const [searchParams] = useSearchParams();
  const newChatProjectId = conversationId ? null : searchParams.get('project');
  const [newChatProject, setNewChatProject] = useState<{ id: string; name: string } | null>(null);
  const newChatProjectName = newChatProject && newChatProject.id === newChatProjectId ? newChatProject.name : null;

  const [messages, setMessages] = useState<Message[]>([]);
  // This chat's answer is still generating after the user left the chat and came back (see answersInProgress).
  const [backgroundAnswer, setBackgroundAnswer] = useState(false);
  const [chatMode, setChatMode] = useState<ChatMode>(DEFAULT_CHAT_MODE);
  const [isLoading, setIsLoading] = useState(false);
  const [streamingContent, setStreamingContent] = useState<string | null>(null);
  const [streamingStage, setStreamingStage] = useState<string | null>(null);
  const [streamingMeta, setStreamingMeta] = useState<StreamMetadata | null>(null);
  const [isUserScrolledUp, setIsUserScrolledUp] = useState(false);
  // Savings popup of the last completed answer.
  const [savedToast, setSavedToast] = useState<TokenUsage | null>(null);
  const [tokenBalance, setTokenBalance] = useState<TokenWallet | null>(() => recallTokenWallet(user));
  const closeSavedToast = useCallback(() => setSavedToast(null), []);

  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  // Server-issued id of the running stream; required to save a stopped answer.
  const streamIdRef = useRef<string | null>(null);
  const lastUserPromptRef = useRef<string>('');
  const justCreatedChatIdRef = useRef<string | null>(null);
  // The answer stream shown on screen. Leaving the chat detaches it (it keeps running in the background);
  // only its own callbacks while it is still this object may change what the page shows.
  const activeStreamRef = useRef<object | null>(null);
  const conversationIdRef = useRef<string | undefined>(conversationId);

  const accumulatedTargetRef = useRef<string>('');
  const displayedTextRef = useRef<string>('');
  const tickerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pendingDoneRef = useRef<{
    meta: StreamMetadata;
    finalText?: string;
    targetChatId: string;
  } | null>(null);

  const isAutoScrollingRef = useRef(false);

  const scrollToBottom = (instant = false) => {
    if (!messagesContainerRef.current) return;
    if (instant) {
      if (!isAutoScrollingRef.current) {
        isAutoScrollingRef.current = true;
        requestAnimationFrame(() => {
          if (messagesContainerRef.current) {
            messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
          }
          isAutoScrollingRef.current = false;
        });
      }
    } else {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  };

  // Prevent browser from navigating to file/folder when dropped outside the dropzone
  useEffect(() => {
    const handleWindowDragOver = (e: DragEvent) => e.preventDefault();
    const handleWindowDrop = (e: DragEvent) => e.preventDefault();

    window.addEventListener('dragover', handleWindowDragOver);
    window.addEventListener('drop', handleWindowDrop);
    return () => {
      window.removeEventListener('dragover', handleWindowDragOver);
      window.removeEventListener('drop', handleWindowDrop);
    };
  }, []);

  // Auto-scroll smoothly without jitter on content updates unless user has scrolled up
  useEffect(() => {
    if (!isUserScrolledUp) {
      scrollToBottom(!!streamingContent);
    }
  }, [messages, streamingContent, streamingStage, isUserScrolledUp]);

  const handleScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const target = e.currentTarget;
    const distanceFromBottom = target.scrollHeight - target.scrollTop - target.clientHeight;
    // If more than 90px from bottom, pause auto-scroll
    setIsUserScrolledUp(distanceFromBottom > 90);
  };

  // Load messages when conversationId changes.
  useEffect(() => {
    conversationIdRef.current = conversationId;
    if (conversationId && justCreatedChatIdRef.current === conversationId) {
      // Don't reset state if this navigation was triggered by creating a chat for the current stream
      justCreatedChatIdRef.current = null;
      return;
    }

    // Opening another chat or starting a new one no longer stops the answer: it keeps generating in the
    // background and is saved; coming back shows "Still generating" until it arrives.
    if (activeStreamRef.current) {
      activeStreamRef.current = null;
      if (tickerRef.current) {
        clearInterval(tickerRef.current);
        tickerRef.current = null;
      }
      pendingDoneRef.current = null;
      abortControllerRef.current = null;
      streamIdRef.current = null;
      setIsLoading(false);
    }
    setBackgroundAnswer(answersInProgress.isRunning(conversationId));
    setStreamingContent(null);
    setStreamingStage(null);
    setStreamingMeta(null);
    setSavedToast(null);
    setIsUserScrolledUp(false);
    setChatMode(DEFAULT_CHAT_MODE);

    if (conversationId) {
      const failure = answersInProgress.takeFailure(conversationId);
      apiGetMessages(conversationId)
        .then((loaded) => setMessages(failure ? [...loaded, failureMessage(conversationId, failure)] : loaded))
        .catch((err) => {
          console.error('Failed to load messages for chat:', conversationId, err);
          setMessages([]);
        });
    } else {
      setMessages([]);
    }
  }, [conversationId]);

  // Leaving the page (Settings, a project, the hub) detaches the answer: it keeps generating and is saved.
  useEffect(() => {
    return () => {
      activeStreamRef.current = null;
      if (tickerRef.current) {
        clearInterval(tickerRef.current);
        tickerRef.current = null;
      }
    };
  }, []);

  // The project named in /app?project=<id>; an id that is not one of this account's projects starts an
  // ordinary chat instead.
  useEffect(() => {
    if (!newChatProjectId || !user?.id) return;
    let cancelled = false;
    projectService
      .list(user.id)
      .then((projects) => {
        if (cancelled) return;
        const project = projects.find((item) => item.id === newChatProjectId);
        if (project) setNewChatProject({ id: project.id, name: project.name });
        else navigate('/app', { replace: true });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [newChatProjectId, user?.id, navigate]);

  // An answer that finished while this chat was not the one streaming on screen: show what was saved.
  useEffect(
    () =>
      answersInProgress.subscribe((chatId) => {
        if (chatId !== conversationIdRef.current || activeStreamRef.current) return;
        const failure = answersInProgress.takeFailure(chatId);
        apiGetMessages(chatId)
          .then((loaded) => {
            if (conversationIdRef.current !== chatId) return;
            setMessages(failure ? [...loaded, failureMessage(chatId, failure)] : loaded);
          })
          .catch(() => undefined)
          .finally(() => setBackgroundAnswer(false));
      }),
    [],
  );

  const handleStopGeneration = () => {
    if (tickerRef.current) {
      clearInterval(tickerRef.current);
      tickerRef.current = null;
    }
    pendingDoneRef.current = null;
    activeStreamRef.current = null;
    if (conversationId) answersInProgress.remove(conversationId);

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setIsLoading(false);

    // Preserve partially generated text in chat history and database (registered users only)
    const currentStream = displayedTextRef.current || streamingContent;
    if (currentStream !== null) {
      const stoppedContent = currentStream.trim() ? currentStream : 'Generation stopped.';
      const stoppedAssistantMsg: Message = {
        id: streamingMeta?.message_id || `stopped-${Date.now()}`,
        chat_id: conversationId || '',
        role: 'assistant',
        content: stoppedContent,
        source_type: 'interrupted',
        grounding_score: streamingMeta?.grounding_score,
        created_at: new Date().toISOString(),
        citations: streamingMeta?.citations || [],
        web_sources: streamingMeta?.web_sources || [],
        images: streamingMeta?.images || [],
      };

      setMessages((prev) => [...prev, stoppedAssistantMsg]);

      if (conversationId && stoppedContent.trim() && streamIdRef.current) {
        apiSavePartialMessage(conversationId, stoppedContent, streamIdRef.current).catch(() => {});
      }
    }

    setStreamingContent(null);
    setStreamingStage(null);
    setStreamingMeta(null);
  };

  // Stop for an answer still generating after the user came back to its chat: cancel it on the server by
  // saving what it produced so far, then show the stored messages.
  const handleStopBackgroundAnswer = () => {
    if (!conversationId) return;
    const chatId = conversationId;
    const entry = answersInProgress.stop(chatId);
    setBackgroundAnswer(false);
    const saved = entry?.streamId
      ? apiSavePartialMessage(chatId, entry.text.trim() ? entry.text : 'Generation stopped.', entry.streamId).catch(() => undefined)
      : Promise.resolve();
    void saved.then(() =>
      apiGetMessages(chatId)
        .then((loaded) => {
          if (conversationIdRef.current === chatId) setMessages(loaded);
        })
        .catch(() => undefined),
    );
  };

  // Switching modes applies to the next question; a running answer keeps going.
  const handleModeChange = (nextMode: ChatMode) => {
    if (nextMode === chatMode) return;
    setChatMode(nextMode);
  };

  const handleSendMessage = async (content: string, attachments?: DocumentAttachment[]) => {
    // One answer streams at a time in a chat; a second stream would share the reveal ticker and refs.
    if (isLoading || backgroundAnswer) return;

    lastUserPromptRef.current = content;
    let targetChatId = conversationId;

    const displayContent =
      attachments && attachments.length > 0 && !content.startsWith('[Attached')
        ? `[Attached: ${attachments.map((a) => `${a.name} (${a.page_count || 1} pages)`).join(', ')}]\n\n${content}`
        : content;

    // Show the prompt before any request, so a failure below can never make it disappear.
    const tempUserMsg: Message = {
      id: `user-${Date.now()}`,
      chat_id: targetChatId || '',
      role: 'user',
      content: displayContent,
      attachments,
      created_at: new Date().toISOString(),
      citations: [],
      web_sources: [],
    };

    setMessages((prev) => [...prev, tempUserMsg]);
    await streamAnswer(
      () => targetChatId || '',
      tempUserMsg.id,
      attachments && attachments.length > 0 ? 'Analyzing attached document(s)...' : 'Thinking...',
      async (callbacks, signal, isActive) => {
        // A first question starts a chat (inside the project it was opened from, if any); failures are
        // shown in the chat by streamAnswer.
        if (!targetChatId) {
          const title = content.length > 35 ? content.slice(0, 35) + '...' : content;
          const newChat = await apiCreateChat(title, newChatProjectId ?? undefined);
          targetChatId = newChat.id;
          // If the user already went elsewhere, the answer is generated and saved without pulling them back.
          if (isActive()) {
            setMessages((prev) => prev.map((m) => (m.id === tempUserMsg.id ? { ...m, chat_id: newChat.id } : m)));
            justCreatedChatIdRef.current = newChat.id;
            navigate(`/app/chat/${newChat.id}`, { replace: true });
          }
          if (signal.aborted) return;
        }
        await apiStreamMessage(targetChatId, content, callbacks, signal, attachments, chatMode);
      }
    );
  };

  // Rewrites a sent prompt: everything after it is discarded and a new answer is streamed.
  const handleEditMessage = async (messageId: string, newText: string) => {
    const text = newText.trim();
    const targetChatId = conversationId;
    if (!text || isLoading || backgroundAnswer || !targetChatId) return;

    lastUserPromptRef.current = text;
    setMessages((prev) => {
      const idx = prev.findIndex((m) => m.id === messageId);
      if (idx === -1) return prev;
      const original = prev[idx];
      // Files attached to the original prompt stay attached (the backend keeps them too).
      const attachedPrefix = original.content.match(/^\[Attached:\s*.+?\]\n\n/)?.[0] ?? '';
      return [...prev.slice(0, idx), { ...original, content: attachedPrefix + text }];
    });

    await streamAnswer(() => targetChatId, messageId, 'Thinking...', (callbacks, signal) =>
      apiEditMessageStream(targetChatId, messageId, text, callbacks, signal, chatMode)
    );
  };

  // Drives the typewriter reveal for one answer stream and adds the finished answer to the chat.
  const streamAnswer = async (
    // A getter because a first question only gets its chat id once `start` has created the chat.
    getChatId: () => string,
    userMessageId: string,
    initialStage: string,
    start: (callbacks: StreamCallbacks, signal: AbortSignal, isActive: () => boolean) => Promise<void>
  ) => {
    const stream = {};
    activeStreamRef.current = stream;
    const isActive = () => activeStreamRef.current === stream;
    setIsLoading(true);
    setStreamingContent('');
    setStreamingStage(initialStage);
    setStreamingMeta(null);
    setSavedToast(null);
    setIsUserScrolledUp(false);

    accumulatedTargetRef.current = '';
    displayedTextRef.current = '';
    pendingDoneRef.current = null;
    if (tickerRef.current) {
      clearInterval(tickerRef.current);
      tickerRef.current = null;
    }

    // Smooth progressive typewriter / token reveal ticker (ChatGPT feel)
    tickerRef.current = setInterval(() => {
      const target = accumulatedTargetRef.current;
      const current = displayedTextRef.current;

      if (current.length < target.length) {
        const diff = target.length - current.length;
        // Dynamic pacing; everything at once while the tab is in the background.
        const step = revealStep(diff, typeof document !== 'undefined' && document.hidden);
        const nextText = target.slice(0, current.length + step);
        displayedTextRef.current = nextText;
        setStreamingContent(nextText);
      } else if (pendingDoneRef.current) {
        // Stream completed from backend and all characters have smoothly appeared
        if (tickerRef.current) {
          clearInterval(tickerRef.current);
          tickerRef.current = null;
        }

        const { meta, finalText, targetChatId: finalChatId } = pendingDoneRef.current;
        pendingDoneRef.current = null;
        setIsLoading(false);
        abortControllerRef.current = null;
        activeStreamRef.current = null;

        const resolvedContent =
          finalText && finalText.trim().length > 0
            ? finalText
            : displayedTextRef.current || target || '';

        const finalAssistantMsg: Message = {
          id: meta.message_id || `msg-${Date.now()}`,
          chat_id: finalChatId,
          role: 'assistant',
          content: resolvedContent,
          source_type: meta.source_type || 'knowledge_base',
          grounding_score: meta.grounding_score,
          verification_status: meta.verification_status,
          internal_evidence_confidence: meta.internal_evidence_confidence,
          selected_evidence_quality: meta.selected_evidence_quality,
          kb_score: meta.kb_score,
          web_score: meta.web_score,
          winning_score: meta.winning_score,
          source_scores: meta.source_scores,
          is_in_rag_pipeline: meta.is_in_rag_pipeline,
          external_search_used: meta.external_search_used,
          follow_up_questions: meta.follow_up_questions || [],
          token_usage: meta.token_usage,
          created_at: new Date().toISOString(),
          citations: meta.citations || [],
          web_sources: meta.web_sources || [],
          images: meta.images || [],
          llm_provider: meta.llm_provider,
          llm_model: meta.llm_model,
          regeneration_count: meta.regeneration_count,
        };

        setMessages((prev) => {
          if (prev.some((m) => m.id === finalAssistantMsg.id)) {
            return prev.map((m) => (m.id === finalAssistantMsg.id ? finalAssistantMsg : m));
          }
          return [...prev, finalAssistantMsg];
        });

        setStreamingContent(null);
        setStreamingStage(null);
        setStreamingMeta(null);
        if (meta.token_usage) setSavedToast(meta.token_usage);
        if (typeof meta.token_balance === 'number') {
          const wallet = { balance: meta.token_balance, refillAt: tokenBalance?.refillAt ?? user?.token_refill_at };
          setTokenBalance(wallet);
          if (user?.id) rememberTokenWallet(user.id, wallet);
        }
      }
    }, 20);

    const controller = new AbortController();
    abortControllerRef.current = controller;
    streamIdRef.current = null;

    let streamMetaRef: StreamMetadata = {};

    // Every callback records progress in answersInProgress (so the chat can be left and reopened) and only
    // touches the page while this stream is still the one on screen.
    const callbacks: StreamCallbacks = {
      onStreamId: (streamId: string) => {
        const chatId = getChatId();
        if (chatId) {
          answersInProgress.start(chatId, () => controller.abort());
          answersInProgress.setStreamId(chatId, streamId);
        }
        if (isActive()) streamIdRef.current = streamId;
      },
      onUserMessage: (storedId: string) => {
        if (!isActive()) return;
        // Swap the optimistic id for the stored one so this prompt can be edited later.
        setMessages((prev) => prev.map((m) => (m.id === userMessageId ? { ...m, id: storedId } : m)));
      },
      onStatus: (status: StreamStatus) => {
        if (isActive()) setStreamingStage(status.message);
      },
      onMetadata: (meta: StreamMetadata) => {
        streamMetaRef = { ...streamMetaRef, ...meta };
        if (isActive()) setStreamingMeta(streamMetaRef);
      },
      onToken: (token: string) => {
        answersInProgress.append(getChatId(), token);
        if (isActive()) accumulatedTargetRef.current += token;
      },
      onDone: (doneMeta: StreamMetadata, finalText?: string) => {
        answersInProgress.finish(getChatId());
        if (!isActive()) return;
        const effectiveMeta = { ...streamMetaRef, ...doneMeta };
        if (finalText && finalText.length > accumulatedTargetRef.current.length) {
          accumulatedTargetRef.current = finalText;
        }
        pendingDoneRef.current = {
          meta: effectiveMeta,
          finalText,
          targetChatId: getChatId(),
        };
      },
      onError: (err: any) => {
        console.error('Stream error:', err);
        if (!isActive()) {
          // Shown when the user opens this chat again.
          answersInProgress.finish(getChatId(), (err instanceof Error && err.message) || 'Generation interrupted. Please try again.');
          return;
        }
        answersInProgress.finish(getChatId());
        activeStreamRef.current = null;
        if (tickerRef.current) {
          clearInterval(tickerRef.current);
          tickerRef.current = null;
        }
        pendingDoneRef.current = null;
        setIsLoading(false);
        abortControllerRef.current = null;

        // Keep whatever answer text arrived; otherwise say why there is none.
        const resolvedContent =
          accumulatedTargetRef.current.trim() ||
          displayedTextRef.current.trim() ||
          (err instanceof Error && err.message) ||
          'Generation interrupted. Please try again.';

        const errorMsg: Message = {
          id: `err-${Date.now()}`,
          chat_id: getChatId(),
          role: 'assistant',
          content: resolvedContent,
          source_type: 'error',
          grounding_score: streamMetaRef.grounding_score,
          created_at: new Date().toISOString(),
          citations: streamMetaRef.citations || [],
          web_sources: streamMetaRef.web_sources || [],
          images: streamMetaRef.images || [],
        };

        setMessages((prev) => [...prev, errorMsg]);
        setStreamingContent(null);
        setStreamingStage(null);
        setStreamingMeta(null);
      },
    };

    try {
      await start(callbacks, controller.signal, isActive);
    } catch (err) {
      // Anything `start` throws (e.g. the chat could not be created) must not leave the chat loading.
      if (!controller.signal.aborted) callbacks.onError?.(err);
    }
  };

  return (
    <div className="flex-1 flex flex-col h-full min-h-0 min-w-0 bg-[#ffffff] dark:bg-[#000000] relative text-slate-800 dark:text-slate-100 font-sans transition-colors overflow-hidden">
      <div
        ref={messagesContainerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto flex flex-col relative bg-[#ffffff] dark:bg-[#000000] transition-colors"
      >
        {messages.length === 0 && streamingContent === null ? (
          <div className="flex-1 flex items-center justify-center py-6">
            <WelcomeScreen
              onSelectPrompt={(prompt) => handleSendMessage(prompt)}
              mode={chatMode}
              onModeChange={handleModeChange}
            />
          </div>
        ) : (
          <div className="flex-1 pb-4">
            <ChatSavingsLine messages={messages} />
            {messages.map((msg, idx) => {
              const isLast = idx === messages.length - 1;
              // Regenerating re-runs the question before the latest answer through the edit flow.
              const question = idx > 0 ? messages[idx - 1] : undefined;
              const canRegenerate =
                isLast && msg.role === 'assistant' && question?.role === 'user' && isStoredMessage(question);
              return (
                <ChatMessage
                  key={msg.id}
                  message={msg}
                  orgSharedIds={orgSharedIds}
                  tokenWallet={isLast && msg.role === 'assistant' ? tokenBalance : null}
                  onOrgShared={markOrgShared}
                  onSelectFollowUp={(q) => handleSendMessage(q)}
                  onEdit={
                    msg.role === 'user' && isStoredMessage(msg)
                      ? (text) => handleEditMessage(msg.id, text)
                      : undefined
                  }
                  editDisabled={isLoading || backgroundAnswer}
                  onRegenerate={
                    canRegenerate
                      ? () => handleEditMessage(question!.id, question!.content.replace(/^\[Attached:\s*.+?\]\n\n/, ''))
                      : undefined
                  }
                  onRetry={
                    isLast && (msg.source_type === 'error' || msg.source_type === 'interrupted')
                      ? () => handleSendMessage(lastUserPromptRef.current)
                      : undefined
                  }
                />
              );
            })}

            {/* In-flight streaming response with ChatGPT thinking animation & streaming cursor */}
            {streamingContent !== null && (
              <ChatMessage
                message={{
                  id: 'streaming-assistant',
                  chat_id: conversationId || '',
                  role: 'assistant',
                  content: streamingContent,
                  source_type: streamingMeta?.source_type,
                  grounding_score: streamingMeta?.grounding_score,
                  created_at: new Date().toISOString(),
                  citations: streamingMeta?.citations || [],
                  web_sources: streamingMeta?.web_sources || [],
                  images: streamingMeta?.images || [],
                }}
                isStreaming={true}
                streamingStage={streamingStage}
              />
            )}

            {/* This chat's answer is still generating in the background (the user left and came back). */}
            {backgroundAnswer && streamingContent === null && (
              <ChatMessage
                message={{
                  id: 'background-assistant',
                  chat_id: conversationId || '',
                  role: 'assistant',
                  content: '',
                  created_at: new Date().toISOString(),
                  citations: [],
                  web_sources: [],
                }}
                isStreaming={true}
                streamingStage="Still generating this answer…"
              />
            )}

            <div ref={messagesEndRef} />
          </div>
        )}

        {/* Floating "Jump to latest" button when scrolled up */}
        {isUserScrolledUp && (
          <button
            onClick={() => {
              setIsUserScrolledUp(false);
              scrollToBottom(false);
            }}
            className="sticky bottom-4 self-center z-20 flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-white/95 dark:bg-slate-900/95 hover:bg-slate-100 dark:hover:bg-slate-800 text-slate-800 dark:text-slate-200 border border-slate-200 dark:border-slate-800 shadow-xl text-xs font-medium transition cursor-pointer backdrop-blur-md hover:scale-105 active:scale-95"
          >
            <ChevronDown className="w-4 h-4 text-orange-400 animate-bounce" />
            <span>Jump to latest</span>
          </button>
        )}
      </div>

      {newChatProjectName && messages.length === 0 && (
        <p className="chat-project-note" role="status">
          New chat in project <strong>{newChatProjectName}</strong>
        </p>
      )}

      {/* Bottom Input Area */}
      <ChatInput
        onSendMessage={handleSendMessage}
        onStop={backgroundAnswer && !isLoading ? handleStopBackgroundAnswer : handleStopGeneration}
        isLoading={isLoading || backgroundAnswer}
        mode={chatMode}
        onModeChange={handleModeChange}
        initialInput={prefillPrompt}
      />

      <TokenSavedToast usage={savedToast} onClose={closeSavedToast} />

    </div>
  );
};

