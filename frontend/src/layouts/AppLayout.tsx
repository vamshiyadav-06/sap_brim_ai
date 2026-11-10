import React, { useEffect, useMemo, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import {
  Building2,
  FolderPlus,
  LayoutDashboard,
  Folder,
  LogOut,
  PanelLeftClose,
  PanelLeft,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings,
  UserRound,
  X,
  ChevronDown,
  Trash2,
  Sparkles,
  MessageSquare,
  ShieldCheck,
  Pencil,
  Check,
} from 'lucide-react';


import { BrandLogo } from '../components/navbar/BrandLogo';
import { ProjectModal } from '../components/projects/ProjectModal';
import { AssistantGuideModal } from '../components/ui/AssistantGuideModal';
import { useAuth } from '../context/AuthContext';
import { ORG_ADMIN_PATH, ORG_HUB_PATH, isOrgAdmin, isOrgUser, postSignInPath, signOutPathFor } from './accountRouting';
import { apiGetChats, apiCreateChat, apiDeleteChat, apiUpdateChat } from '../services/api';
import { PROJECTS_CHANGED, projectService } from '../services/projectService';
import { userStorageKey } from '../services/userStorage';
import type { Chat, Project } from '../types';

function dateGroup(chat: Chat): string {
  const date = new Date(chat.updated_at || chat.created_at);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return 'Today';
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return 'Earlier';
}

const AccountWorkspace: React.FC = () => {
  const { user, logout } = useAuth();
  const [chatHistoryOpen, setChatHistoryOpen] = useState(() => {
    try {
      return Boolean(user?.id && localStorage.getItem(userStorageKey(user.id, 'chat_history_expanded')) === 'true');
    } catch {
      return false;
    }
  });
  const [projectsOpen, setProjectsOpen] = useState(() => {
    try {
      return Boolean(user?.id && localStorage.getItem(userStorageKey(user.id, 'projects_expanded')) === 'true');
    } catch {
      return false;
    }
  });
  const [query, setQuery] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [desktopCollapsed, setDesktopCollapsed] = useState(false);
  const [projectModalOpen, setProjectModalOpen] = useState(false);
  const [helpModalOpen, setHelpModalOpen] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [editingChatId, setEditingChatId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [isMobile, setIsMobile] = useState(() => typeof window !== 'undefined' && window.innerWidth <= 900);

  const [chats, setChats] = useState<Chat[]>([]);
  const [chatsLoading, setChatsLoading] = useState(true);
  const [projects, setProjects] = useState<Project[]>([]);
  const [pinnedChatIds, setPinnedChatIds] = useState<string[]>([]);

  const location = useLocation();
  const navigate = useNavigate();

  const isConversation = location.pathname === '/app' || location.pathname.includes('/chat/');

  useEffect(() => {
    if (!user?.id) {
      setProjects([]);
      setPinnedChatIds([]);
      return;
    }
    try {
      setPinnedChatIds(JSON.parse(localStorage.getItem(userStorageKey(user.id, 'pinned_chats')) || '[]'));
    } catch {
      setPinnedChatIds([]);
    }
  }, [user?.id]);

  // Projects live on the server; reload them when any page creates, changes or deletes one.
  useEffect(() => {
    if (!user?.id) return;
    const userId = user.id;
    let cancelled = false;
    const load = () => {
      projectService
        .list(userId)
        .then((data) => {
          if (!cancelled) setProjects(data);
        })
        .catch(() => undefined); // keep the list already shown
    };
    load();
    window.addEventListener(PROJECTS_CHANGED, load);
    return () => {
      cancelled = true;
      window.removeEventListener(PROJECTS_CHANGED, load);
    };
  }, [user?.id]);

  // Refresh chats list for the signed-in account.
  const refreshChats = async () => {
    try {
      const data = await apiGetChats();
      setChats(data);
    } catch {
      setChats([]);
    } finally {
      setChatsLoading(false);
    }
  };

  useEffect(() => {
    refreshChats();
  }, [location.pathname]);

  useEffect(() => {
    const applyTheme = () => {
      const pref = localStorage.getItem('clyptus-appearance');
      const media = window.matchMedia('(prefers-color-scheme: dark)');
      const isDark = pref === 'dark' || (pref === 'system' && media.matches);
      document.documentElement.classList.toggle('dark', isDark);
    };
    applyTheme();
    window.addEventListener('storage', applyTheme);
    window.addEventListener('theme-change', applyTheme);
    return () => {
      window.removeEventListener('storage', applyTheme);
      window.removeEventListener('theme-change', applyTheme);
    };
  }, []);

  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth <= 900);
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname, location.search]);

  // Keyboard shortcut: Cmd/Ctrl + K for New Chat
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        handleNewChat();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);

  const normalizedQuery = query.trim().toLowerCase();

  const enrichedChats = useMemo(() => {
    // Chats started inside a project live under that project only, not in the general chat list.
    return chats.filter(c => !c.project_id).map(c => ({
      ...c,
      isPinned: pinnedChatIds.includes(c.id),
    }));
  }, [chats, pinnedChatIds]);

  const filteredChats = useMemo(() => {
    return enrichedChats.filter(chat => !normalizedQuery || chat.title.toLowerCase().includes(normalizedQuery));
  }, [enrichedChats, normalizedQuery]);

  const filteredProjects = useMemo(() => {
    return projects.filter(project => !normalizedQuery || project.name.toLowerCase().includes(normalizedQuery));
  }, [projects, normalizedQuery]);

  const groupedChats = useMemo(() => {
    return ['Today', 'Yesterday', 'Earlier']
      .map(group => ({
        group,
        chats: filteredChats.filter(chat => dateGroup(chat) === group),
      }))
      .filter(group => group.chats.length > 0);
  }, [filteredChats]);

  const pinnedChats = useMemo(() => {
    return enrichedChats.filter(chat => chat.isPinned && (!normalizedQuery || chat.title.toLowerCase().includes(normalizedQuery)));
  }, [enrichedChats, normalizedQuery]);

  const pinnedProjects = useMemo(() => {
    return projects.filter(project => project.isPinned && (!normalizedQuery || project.name.toLowerCase().includes(normalizedQuery)));
  }, [projects, normalizedQuery]);

  const handleLogout = () => {
    const signIn = signOutPathFor(user);
    logout();
    navigate(signIn, { replace: true });
  };

  const handleNewChat = async () => {
    try {
      const newChat = await apiCreateChat('New SAP Chat');
      setChats(prev => [newChat, ...prev]);
      navigate(`/app/chat/${newChat.id}`);
    } catch {
      navigate('/app');
    }
  };

  const handleDeleteChat = async (chatId: string, e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (!window.confirm('Delete this conversation?')) return;
    try {
      await apiDeleteChat(chatId);
      setChats(prev => prev.filter(c => c.id !== chatId));
      setPinnedChatIds(prev => {
        const next = prev.filter(id => id !== chatId);
        if (user?.id) localStorage.setItem(userStorageKey(user.id, 'pinned_chats'), JSON.stringify(next));
        return next;
      });
      if (location.pathname.includes(chatId)) {
        navigate('/app');
      }
    } catch (err) {
      console.error('Failed to delete chat:', err);
      setActionError('Could not delete chat.');
    }
  };

  const handleCreateProject = async (values: { name: string; description?: string }) => {
    try {
      setActionError(null);
      if (!user?.id) throw new Error('Sign in to manage projects.');
      const project = await projectService.create(values);
      setProjectModalOpen(false);
      navigate(`/app/projects/${project.id}`);
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : 'Could not create project.');
    }
  };

  const saveRename = async (chat: Chat) => {
    const title = renameDraft.trim().slice(0, 100);
    setEditingChatId(null);
    if (!title || title === chat.title) return;
    try {
      setActionError(null);
      const updated = await apiUpdateChat(chat.id, title);
      setChats(prev => prev.map(c => (c.id === chat.id ? { ...c, title: updated.title ?? title } : c)));
    } catch {
      setActionError('Could not rename chat.');
    }
  };

  const renderChatRow = (chat: Chat & { isPinned?: boolean }, keyPrefix = '') => {
    const isSelected = location.pathname === `/app/chat/${chat.id}`;
    if (editingChatId === chat.id) {
      return (
        <form
          key={`${keyPrefix}${chat.id}`}
          className="chatgpt-chat-item selected-chat"
          onSubmit={e => {
            e.preventDefault();
            void saveRename(chat);
          }}
        >
          <input
            autoFocus
            aria-label="Chat name"
            maxLength={100}
            value={renameDraft}
            onChange={e => setRenameDraft(e.target.value)}
            onKeyDown={e => e.key === 'Escape' && setEditingChatId(null)}
            className="flex-1 min-w-0 text-sm px-2 py-1 rounded-md bg-transparent border border-orange-500/40 outline-none"
          />
          <button className="item-action-btn" type="submit" aria-label="Save chat name" title="Save">
            <Check size={13} strokeWidth={2.2} />
          </button>
          <button className="item-action-btn" type="button" aria-label="Cancel rename" title="Cancel" onClick={() => setEditingChatId(null)}>
            <X size={13} strokeWidth={2.2} />
          </button>
        </form>
      );
    }
    return (
      <div className={`chatgpt-chat-item ${isSelected ? 'selected-chat' : ''}`} key={`${keyPrefix}${chat.id}`}>
        <NavLink className="chat-item-link" to={`/app/chat/${chat.id}`} title={chat.title}>
          <MessageSquare size={14} strokeWidth={2} className="chat-item-icon" />
          <span className="truncate flex-1">{chat.title}</span>
        </NavLink>
        <div className="chat-item-actions">
          <button
            className="item-action-btn"
            type="button"
            aria-label="Rename chat"
            title="Rename chat"
            onClick={() => {
              setRenameDraft(chat.title);
              setEditingChatId(chat.id);
            }}
          >
            <Pencil size={13} strokeWidth={2} />
          </button>
          <button
            className="item-action-btn"
            type="button"
            aria-label={chat.isPinned ? 'Unpin chat' : 'Pin chat'}
            title={chat.isPinned ? 'Unpin chat' : 'Pin chat'}
            onClick={() => toggleChatPin(chat.id)}
          >
            {chat.isPinned ? <PinOff size={13} strokeWidth={2} /> : <Pin size={13} strokeWidth={2} />}
          </button>
          <button
            className="item-action-btn hover:text-rose-500"
            type="button"
            aria-label="Delete chat"
            title="Delete chat"
            onClick={(e) => handleDeleteChat(chat.id, e)}
          >
            <Trash2 size={13} strokeWidth={2} />
          </button>
        </div>
      </div>
    );
  };

  const toggleChatPin = (chatId: string) => {
    setPinnedChatIds(prev => {
      const updated = prev.includes(chatId) ? prev.filter(id => id !== chatId) : [...prev, chatId];
      if (user?.id) localStorage.setItem(userStorageKey(user.id, 'pinned_chats'), JSON.stringify(updated));
      return updated;
    });
  };

  const toggleProjectPin = async (projectId: string, currentPin: boolean) => {
    try {
      if (!user?.id) return;
      await projectService.update(projectId, { isPinned: !currentPin });
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : 'Could not update pin.');
    }
  };

  const toggleSidebar = () => {
    if (isMobile) {
      setSidebarOpen(prev => !prev);
    } else {
      setDesktopCollapsed(prev => !prev);
    }
  };

  const userInitial = user?.name ? user.name.charAt(0).toUpperCase() : 'C';

  return (
    <div className={`app-shell ${desktopCollapsed ? 'desktop-collapsed' : ''}`}>
      {sidebarOpen && (
        <button
          className="sidebar-backdrop"
          type="button"
          aria-label="Close sidebar"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* ChatGPT-Style Sidebar */}
      <aside className={`app-sidebar ${sidebarOpen ? 'app-sidebar-open' : ''}`} aria-label="Workspace sidebar">
        {/* Top Header Row with Logo & Collapse Toggle */}
        <div className="sidebar-top-header">
          {/* Signed in: the logo stays in the app (never the landing or organisation pages). */}
          <BrandLogo size="lg" to={postSignInPath(user)} />
          <div className="flex items-center gap-1 ml-auto">
            {isMobile ? (
              <button
                className="sidebar-icon-btn"
                type="button"
                aria-label="Close sidebar"
                onClick={() => setSidebarOpen(false)}
              >
                <X size={18} strokeWidth={2.2} />
              </button>
            ) : (
              <button
                className="sidebar-icon-btn"
                type="button"
                aria-label="Collapse sidebar"
                title="Close sidebar"
                onClick={toggleSidebar}
              >
                <PanelLeftClose size={18} strokeWidth={2.2} />
              </button>
            )}
          </div>
        </div>

        {/* Prominent ChatGPT-Style "New Chat" Button */}
        <button
          className="chatgpt-new-chat-btn"
          type="button"
          onClick={handleNewChat}
          title="New chat (⌘K)"
        >
          <div className="flex items-center gap-2.5 min-w-0">
            <div className="w-6 h-6 rounded-lg bg-orange-500/10 dark:bg-orange-500/20 text-orange-600 dark:text-orange-400 flex items-center justify-center shrink-0">
              <Plus size={16} strokeWidth={2.6} />
            </div>
            <span className="font-bold text-sm text-slate-900 dark:text-white truncate">New chat</span>
          </div>
          <span className="chatgpt-kbd">⌘K</span>
        </button>

        {/* ChatGPT Minimalist Search Bar */}
        <label className="chatgpt-search-bar">
          <Search size={15} strokeWidth={2.2} className="text-slate-400 dark:text-zinc-500 shrink-0" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search chats..."
            aria-label="Search chats"
          />
          {query ? (
            <button
              type="button"
              onClick={() => setQuery('')}
              className="text-slate-400 hover:text-slate-600 dark:hover:text-white p-0.5"
            >
              <X size={12} />
            </button>
          ) : (
            <kbd className="chatgpt-kbd-slash">/</kbd>
          )}
        </label>

        {actionError && <p className="sidebar-error" role="alert">{actionError}</p>}

        {/* Chat History Section */}
        <div className="sidebar-history-area">
          {/* Pinned Section */}
          {(pinnedChats.length > 0 || pinnedProjects.length > 0) && (
            <div className="sidebar-group">
              <span className="sidebar-group-title flex items-center gap-1.5">
                <Pin size={11} strokeWidth={2.4} className="text-orange-500" />
                <span>Pinned</span>
              </span>
              {pinnedChats.map((chat) => renderChatRow(chat, 'pinned-'))}
              {pinnedProjects.map((project) => (
                <div className="chatgpt-chat-item" key={`pinned-proj-${project.id}`}>
                  <NavLink
                    className="chat-item-link"
                    to={`/app/projects/${project.id}`}
                    title={project.name}
                  >
                    <Folder size={14} strokeWidth={2} className="chat-item-icon text-orange-500" />
                    <span className="truncate flex-1">{project.name}</span>
                  </NavLink>
                  <div className="chat-item-actions">
                    <button
                      className="item-action-btn"
                      type="button"
                      aria-label="Unpin project"
                      title="Unpin project"
                      onClick={() => toggleProjectPin(project.id, project.isPinned)}
                    >
                      <PinOff size={13} strokeWidth={2} />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          <details
            className="sidebar-chats"
            open={chatHistoryOpen}
            onToggle={(event) => {
              const open = event.currentTarget.open;
              setChatHistoryOpen(open);
              try {
                if (user?.id) localStorage.setItem(userStorageKey(user.id, 'chat_history_expanded'), String(open));
              } catch {
                // Keep the dropdown usable when browser storage is unavailable.
              }
            }}
          >
            <summary className="sidebar-group-title sidebar-chats-toggle" aria-controls="sidebar-chat-list">
              <span>Chats</span>
              <ChevronDown size={14} strokeWidth={2.2} aria-hidden="true" />
            </summary>
            <div id="sidebar-chat-list">
              {/* Grouped Chats (Today, Yesterday, Earlier) */}
              {groupedChats.map(({ group, chats: items }) => (
                <div className="sidebar-group" key={group}>
                  <span className="sidebar-group-title">{group}</span>
                  {items.map((chat) => renderChatRow(chat))}
                </div>
              ))}

              {!chatsLoading && groupedChats.length === 0 && pinnedChats.length === 0 && (
                <div className="sidebar-empty-state">
                  <p>{normalizedQuery ? 'No matching conversations.' : 'Previous chats will appear here.'}</p>
                </div>
              )}
            </div>
          </details>

          {/* Projects Section */}
          <div className="sidebar-group mt-3">
            <div className="flex items-center justify-between gap-2 px-2 py-1">
              <button
                type="button"
                className="sidebar-group-title sidebar-chats-toggle flex-1 !p-0"
                aria-expanded={projectsOpen}
                aria-controls="sidebar-project-list"
                onClick={() => {
                  const open = !projectsOpen;
                  setProjectsOpen(open);
                  try {
                    if (user?.id) localStorage.setItem(userStorageKey(user.id, 'projects_expanded'), String(open));
                  } catch {
                    // Keep the dropdown usable when browser storage is unavailable.
                  }
                }}
              >
                <span>Projects</span>
                <ChevronDown size={14} strokeWidth={2.2} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="text-slate-400 hover:text-slate-700 dark:hover:text-white p-1 rounded hover:bg-slate-200/60 dark:hover:bg-zinc-800 transition cursor-pointer"
                aria-label="Create a project"
                title="Create a project"
                onClick={() => setProjectModalOpen(true)}
              >
                <FolderPlus size={14} strokeWidth={2.2} />
              </button>
            </div>

            <div id="sidebar-project-list" hidden={!projectsOpen}>
              {filteredProjects.map((project) => (
                <div className="chatgpt-chat-item" key={project.id}>
                  <NavLink
                    className="chat-item-link"
                    to={`/app/projects/${project.id}`}
                    title={project.name}
                  >
                    <Folder size={14} strokeWidth={2} className="chat-item-icon text-orange-500" />
                    <span className="truncate flex-1">{project.name}</span>
                  </NavLink>
                  <div className="chat-item-actions">
                    <button
                      className="item-action-btn"
                      type="button"
                      aria-label={project.isPinned ? 'Unpin project' : 'Pin project'}
                      title={project.isPinned ? 'Unpin project' : 'Pin project'}
                      onClick={() => toggleProjectPin(project.id, project.isPinned)}
                    >
                      {project.isPinned ? <PinOff size={13} strokeWidth={2} /> : <Pin size={13} strokeWidth={2} />}
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Bottom Section with Settings and User Profile Icons */}
        <div className="sidebar-bottom-section">
          {/* Guide / Help item */}
          <button
            className="chatgpt-bottom-row-btn"
            type="button"
            onClick={() => setHelpModalOpen(true)}
            title="SAP BRIM Knowledge Assistant Guide"
          >
            <Sparkles size={16} strokeWidth={2.2} className="text-orange-500 shrink-0" />
            <span className="font-bold text-xs text-slate-800 dark:text-zinc-200 truncate">
              SAP + BRIM Guide
            </span>
          </button>

          {/* Organisation accounts: the shared Knowledge Hub; owners and admins also get their dashboard.
              Individual users have no organisation shortcut (removed 2026-11-03). */}
          {isOrgUser(user) && (
            <NavLink
              to={ORG_HUB_PATH}
              end
              className={({ isActive }) => `chatgpt-bottom-row-btn ${isActive ? 'active-bottom-btn' : ''}`}
              title="Your organisation's shared SAP BRIM answers"
            >
              <Building2 size={16} strokeWidth={2.2} className="text-orange-500 shrink-0" />
              <span className="font-bold text-xs text-slate-800 dark:text-zinc-200 truncate">Knowledge Hub</span>
            </NavLink>
          )}
          {isOrgAdmin(user) && (
            <NavLink
              to={ORG_ADMIN_PATH}
              className={({ isActive }) => `chatgpt-bottom-row-btn ${isActive ? 'active-bottom-btn' : ''}`}
              title="Manage members, activity and organisation settings"
            >
              <LayoutDashboard size={16} strokeWidth={2.2} className="text-orange-500 shrink-0" />
              <span className="font-bold text-xs text-slate-800 dark:text-zinc-200 truncate">Admin Dashboard</span>
            </NavLink>
          )}

          {/* Admin Dashboard link - strictly restricted to Super Admin */}
          {user?.is_admin && (
            <NavLink
              to="/admin"
              className={({ isActive }) =>
                `chatgpt-bottom-row-btn ${isActive ? 'active-bottom-btn' : ''} bg-amber-500/10 hover:bg-amber-500/20 text-amber-600 dark:text-amber-400 border border-amber-500/30`
              }
              title="Super Admin Knowledge Base & Document Manager"
            >
              <ShieldCheck size={16} strokeWidth={2.2} className="text-amber-500 shrink-0" />
              <span className="font-bold text-xs truncate">
                Admin Dashboard
              </span>
            </NavLink>
          )}

          {/* Settings link */}
          <NavLink
            to="/app/settings"
            className={({ isActive }) =>
              `chatgpt-bottom-row-btn ${isActive ? 'active-bottom-btn' : ''}`
            }
            title="Account and Workspace Settings"
          >
            <Settings size={16} strokeWidth={2.2} className="text-slate-600 dark:text-zinc-400 shrink-0" />
            <span className="font-bold text-xs text-slate-800 dark:text-zinc-200 truncate">
              Settings
            </span>
          </NavLink>

          {/* User Profile Card */}
          <div className="relative mt-1">
            <button
              className="chatgpt-profile-btn"
              type="button"
              onClick={() => setMenuOpen(!menuOpen)}
              aria-expanded={menuOpen}
            >
              <span className="chatgpt-avatar shrink-0">
                {user?.avatar ? <img src={user.avatar} alt="" /> : userInitial}
              </span>
              <div className="flex flex-col text-left min-w-0 flex-1 leading-tight">
                <strong className="font-bold text-xs text-slate-900 dark:text-white truncate">
                  {user?.name || 'SAP Consultant'}
                </strong>
                <small className="text-[10px] font-semibold text-slate-500 dark:text-zinc-400 truncate">
                  {user?.email || 'Enterprise AI User'}
                </small>
              </div>
              <ChevronDown size={14} strokeWidth={2.2} className="text-slate-400 shrink-0" />
            </button>

            {menuOpen && (
              <div className="chatgpt-profile-menu">
                <NavLink to="/app/profile" onClick={() => setMenuOpen(false)}>
                  <UserRound size={15} strokeWidth={2} />
                  <span className="font-semibold text-xs">Your Profile</span>
                </NavLink>
                {isOrgUser(user) && (
                  <NavLink to={ORG_HUB_PATH} end onClick={() => setMenuOpen(false)}>
                    <Building2 size={15} strokeWidth={2} />
                    <span className="font-semibold text-xs">Knowledge Hub</span>
                  </NavLink>
                )}
                {isOrgAdmin(user) && (
                  <NavLink to={ORG_ADMIN_PATH} onClick={() => setMenuOpen(false)}>
                    <LayoutDashboard size={15} strokeWidth={2} />
                    <span className="font-semibold text-xs">Admin Dashboard</span>
                  </NavLink>
                )}
                {user?.is_admin && (
                  <NavLink to="/admin" onClick={() => setMenuOpen(false)} className="text-amber-600 dark:text-amber-400 font-bold">
                    <ShieldCheck size={15} strokeWidth={2} />
                    <span className="font-semibold text-xs">Admin Dashboard</span>
                  </NavLink>
                )}
                <NavLink to="/app/settings" onClick={() => setMenuOpen(false)}>
                  <Settings size={15} strokeWidth={2} />
                  <span className="font-semibold text-xs">Settings & Theme</span>
                </NavLink>
                <div className="h-px bg-slate-200 dark:bg-zinc-800 my-1" />
                <button type="button" onClick={handleLogout} className="text-rose-600 dark:text-rose-400">
                  <LogOut size={15} strokeWidth={2} />
                  <span className="font-semibold text-xs">Sign out</span>
                </button>
              </div>
            )}
          </div>

        </div>
      </aside>

      {/* Main Container */}
      <main className={`app-main ${isConversation ? 'app-main-chat' : 'app-main-page'}`}>
        {isConversation ? (
          <header className="chatgpt-topbar">
            <div className="flex items-center gap-2">
              {/* Only show the open sidebar toggle when sidebar is collapsed on desktop, or when on mobile */}
              {(desktopCollapsed || isMobile) && (
                <button
                  className="chatgpt-topbar-toggle-btn"
                  type="button"
                  aria-label="Open sidebar"
                  title="Open sidebar"
                  onClick={toggleSidebar}
                >
                  <PanelLeft size={18} strokeWidth={2.2} />
                </button>
              )}

              {/* Seamless Model / Assistant Title (ChatGPT style, no frozen bar) */}
              <div className="flex items-center gap-2 px-2 py-1 rounded-xl hover:bg-black/5 dark:hover:bg-white/5 transition cursor-default select-none">
                <span className="text-sm font-bold text-slate-900 dark:text-white tracking-tight">
                  SAP BRIM Assistant
                </span>
                <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-orange-500/10 dark:bg-orange-500/20 text-orange-600 dark:text-orange-400 border border-orange-500/25 uppercase tracking-wide">
                  Enterprise AI
                </span>
              </div>
            </div>

            {/* Right side New chat button */}
            <div className="ml-auto flex items-center gap-2">
              <button
                className="chatgpt-topbar-new-chat-btn"
                type="button"
                onClick={handleNewChat}
                title="New chat (⌘K)"
              >
                <Plus size={15} strokeWidth={2.6} className="text-orange-500" />
                <span className="hidden sm:inline font-bold text-xs">New chat</span>
              </button>
            </div>
          </header>
        ) : (
          <header className="app-topbar">
            <div className="app-topbar-left">
              <button
                className={`icon-button ${desktopCollapsed ? 'inline-flex' : 'app-mobile-menu'}`}
                type="button"
                aria-label="Toggle sidebar"
                title={desktopCollapsed ? 'Open sidebar' : 'Toggle sidebar'}
                onClick={toggleSidebar}
              >
                <PanelLeft size={19} strokeWidth={2.2} />
              </button>
              <div className="app-breadcrumb">
                <strong className="font-bold text-sm text-slate-900 dark:text-white">
                  {location.pathname === ORG_HUB_PATH
                    ? 'Knowledge Hub'
                    : location.pathname === ORG_ADMIN_PATH
                      ? 'Admin Dashboard'
                      : location.pathname.split('/').pop()?.replace(/^\w/, (c) => c.toUpperCase()) || 'Workspace'}
                </strong>
              </div>
            </div>
            <div className="app-top-actions">
              <button className="top-new-chat font-bold text-xs" type="button" onClick={handleNewChat}>
                <Plus size={14} strokeWidth={2.4} />
                New chat
              </button>
              <span className="top-avatar">
                {user?.avatar ? <img src={user.avatar} alt="" /> : userInitial}
              </span>
            </div>
          </header>
        )}

        {/* Content Route */}
        <Outlet />
      </main>

      {projectModalOpen && (
        <ProjectModal
          error={actionError}
          onClose={() => {
            setProjectModalOpen(false);
            setActionError(null);
          }}
          onSave={handleCreateProject}
        />
      )}

      <AssistantGuideModal
        isOpen={helpModalOpen}
        onClose={() => setHelpModalOpen(false)}
      />
    </div>
  );
};

export const AppLayout: React.FC = () => {
  const { user } = useAuth();
  // Remount the workspace and its routes when accounts change, discarding the
  // previous account's in-memory projects, pins and pending chat-list results.
  return <AccountWorkspace key={user?.id ?? 'signed-out'} />;
};
