import React, {
  createContext,
  useContext,
  useState,
  useEffect,
  useRef,
} from 'react';

import type { AuthTokenResponse, User } from '../types';

import {
  ApiError,
  apiGetMe,
  apiLogout,
  apiLogin,
  apiAdminLogin,
  apiOrgLogin,
  apiRegister,
  getStoredToken,
  setStoredToken,
  clearStoredToken,
  setUnauthorizedHandler,
  TOKEN_STORAGE_KEY,
} from '../services/api';
import { clearAllUserStorage, clearLegacyUserStorage, clearUserStorage, userStorageKey } from '../services/userStorage';
import { signInPathFor } from '../layouts/accountRouting';

const cachedProfileForToken = (accessToken: string): User | null => {
  try {
    const payload = accessToken.split('.')[1];
    if (!payload) return null;
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/');
    const claims = JSON.parse(window.atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')));
    if (typeof claims.sub !== 'string' || !claims.sub) return null;
    const cached = localStorage.getItem(userStorageKey(claims.sub, 'profile'));
    if (!cached) return null;
    const profile = JSON.parse(cached) as User;
    return profile.id === claims.sub ? { ...profile, profileCompleted: true } : null;
  } catch {
    // The cached profile is only a temporary display fallback while the server is unreachable.
    return null;
  }
};

interface AuthContextType {
  user: User | null;
  token: string | null;
  loading: boolean;
  login: (email: string, password: string, unlockKey?: string) => Promise<void>;
  adminLogin: (email: string, password: string, unlockKey?: string) => Promise<void>;
  /** Organisation accounts sign in through the Organisation Portal. */
  orgLogin: (email: string, password: string, unlockKey?: string) => Promise<User>;
  /** Stores a session returned by activation or a password change (the server revoked the old token). */
  applySession: (session: AuthTokenResponse) => User;
  register: (name: string, email: string, password: string) => Promise<void>;
  updateProfile: (updates: { name: string; dateOfBirth?: string; avatar?: string }) => void;
  /** Remembers the answer language the server accepted. */
  setPreferredLanguage: (code: string) => void;
  setQuestionLanguage: (code: string | null) => void;
  logout: () => void;
}


const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const AuthProvider: React.FC<{
  children: React.ReactNode;
}> = ({ children }) => {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(getStoredToken());
  const [loading, setLoading] = useState<boolean>(true);
  const userIdRef = useRef<string | null>(null);
  // Remembered so an expired organisation session returns to the Organisation Portal sign-in.
  const accountTypeRef = useRef<string | null>(null);

  const saveUser = (next: User) => {
    userIdRef.current = next.id;
    accountTypeRef.current = next.account_type ?? null;
    localStorage.setItem(userStorageKey(next.id, 'profile'), JSON.stringify(next));
    setUser(next);
  };

  const clearCurrentUserStorage = () => {
    if (userIdRef.current) clearUserStorage(userIdRef.current);
    clearLegacyUserStorage();
    userIdRef.current = null;
  };

  useEffect(() => {
    const initAuth = async () => {
      clearLegacyUserStorage();
      const stored = getStoredToken();

      if (stored) {
        try {
          const userData = await apiGetMe();
          userIdRef.current = userData.id;
          const savedLocalProfile = localStorage.getItem(userStorageKey(userData.id, 'profile'));
          let extra: { dateOfBirth?: string; avatar?: string } = {};
          if (savedLocalProfile) {
            try {
              const cached = JSON.parse(savedLocalProfile);
              extra = { dateOfBirth: cached.dateOfBirth, avatar: cached.avatar };
            } catch {}
          }
          saveUser({ ...userData, profileCompleted: true, ...extra });
          setToken(getStoredToken() ?? stored);
        } catch (error) {
          console.error('Authentication initialization failed:', error);
          const status = error instanceof ApiError ? error.status : undefined;
          if (status === 401 || status === 403) {
            // The server rejected the token (expired or signed with an old secret). Restoring the
            // cached profile would look signed in while every chat request fails, so sign out.
            clearStoredToken();
            clearAllUserStorage();
            userIdRef.current = null;
            setToken(null);
            setUser(null);
          } else {
            // A network failure or 5xx does not prove the session is invalid. Preserve the refresh
            // cookie and local session so the next authenticated request can retry after recovery.
            const cachedProfile = cachedProfileForToken(stored);
            if (cachedProfile) {
              userIdRef.current = cachedProfile.id;
              accountTypeRef.current = cachedProfile.account_type ?? null;
              setUser(cachedProfile);
            }
            setToken(stored);
          }
        }
      } else {
        setUser(null);
      }

      setLoading(false);
    };

    initAuth();
  }, []);

  // An authenticated request answered 401: the session ended (expiry, logout elsewhere, revoked).
  // Clear it everywhere and go back to the right sign-in page instead of failing request by request.
  useEffect(() => {
    let handled = false;
    setUnauthorizedHandler(() => {
      if (handled) return;
      handled = true;
      clearStoredToken();
      clearCurrentUserStorage();
      setToken(null);
      setUser(null);
      const signIn = signInPathFor(window.location.pathname, accountTypeRef.current);
      if (!window.location.pathname.startsWith(signIn)) {
        window.location.assign(`${signIn}?expired=1`);
      }
    });
    // Signing out (or in as someone else) in another tab ends this tab's session too.
    const onStorage = (event: StorageEvent) => {
      if (event.key !== TOKEN_STORAGE_KEY) return;
      if (!event.newValue) {
        clearCurrentUserStorage();
        setToken(null);
        setUser(null);
      } else if (event.oldValue && event.newValue !== event.oldValue) {
        window.location.reload();
      }
    };
    window.addEventListener('storage', onStorage);
    return () => {
      setUnauthorizedHandler(null);
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const login = async (
    email: string,
    password: string,
    unlockKey?: string,
  ): Promise<void> => {
    const data = await apiLogin(email, password, unlockKey);

    setStoredToken(data.access_token);
    setToken(data.access_token);
    const enrichedUser: User = {
      ...data.user,
      profileCompleted: true,
    };
    saveUser(enrichedUser);
  };

  const adminLogin = async (
    email: string,
    password: string,
    unlockKey?: string,
  ): Promise<void> => {
    const data = await apiAdminLogin(email, password, unlockKey);

    setStoredToken(data.access_token);
    setToken(data.access_token);
    const enrichedUser: User = {
      ...data.user,
      profileCompleted: true,
    };
    saveUser(enrichedUser);
  };


  const applySession = (session: AuthTokenResponse): User => {
    setStoredToken(session.access_token);
    setToken(session.access_token);
    const enrichedUser: User = {
      ...session.user,
      profileCompleted: true,
    };
    saveUser(enrichedUser);
    return enrichedUser;
  };

  const orgLogin = async (email: string, password: string, unlockKey?: string): Promise<User> => {
    const data = await apiOrgLogin(email, password, unlockKey);
    return applySession(data);
  };

  // Creates the account only; the user signs in afterwards (owner decision 2026-11-04).
  const register = async (name: string, email: string, password: string): Promise<void> => {
    await apiRegister(name, email, password);
  };

  const setPreferredLanguage = (code: string) => {
    if (user) saveUser({ ...user, preferred_language: code });
  };

  const setQuestionLanguage = (code: string | null) => {
    if (user) saveUser({ ...user, question_language: code });
  };

  const updateProfile = (updates: { name: string; dateOfBirth?: string; avatar?: string }) => {
    if (!user) return;
    saveUser({ ...user, ...updates, profileCompleted: true });
  };

  const logout = (): void => {
    const tokenBeforeLogout = getStoredToken();
    void apiLogout().catch(() => undefined).finally(() => {
      // A refresh can finish just as the user logs out. Do not leave that replacement access token behind.
      if (getStoredToken() !== tokenBeforeLogout) clearStoredToken();
    });
    clearStoredToken();
    clearCurrentUserStorage();
    setToken(null);
    setUser(null);
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        loading,
        login,
        adminLogin,
        orgLogin,
        applySession,
        register,
        updateProfile,
        setPreferredLanguage,
        setQuestionLanguage,
        logout,
      }}

    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = (): AuthContextType => {
  const context = useContext(AuthContext);

  if (!context) {
    throw new Error(
      'useAuth must be used within an AuthProvider'
    );
  }

  return context;
};
