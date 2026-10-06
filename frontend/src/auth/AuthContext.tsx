import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  ACCESS_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
  fetchMe,
  getActiveTenantId,
  login as loginRequest,
  logout as logoutRequest,
  setActiveTenantId,
  setUnauthorizedHandler,
  type CurrentUser,
  type UserRole,
} from "../api/client";
import { decodeAccessToken, isExpired } from "./jwt";

interface AuthState {
  /** null while bootstrapping, then either a user or null when signed out. */
  user: CurrentUser | null;
  /** Optimistic role from the JWT, available before `/auth/me` resolves. */
  role: UserRole | undefined;
  /** The tenant the UI is currently showing: own tenant, or the impersonated
   *  one for a super admin using the tenant switcher. */
  effectiveTenantId: string | null;
  /** Non-null only when a super admin is inspecting someone else's spa. */
  impersonatedTenantId: string | null;
  loading: boolean;
  error: string | null;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => void;
  inspectTenant: (tenantId: string | null) => void;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

const readStoredClaims = () => {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (!token) return null;
  const claims = decodeAccessToken(token);
  return claims && !isExpired(claims) ? claims : null;
};

export function AuthProvider({ children }: { children: ReactNode }) {
  const stored = readStoredClaims();

  const [user, setUser] = useState<CurrentUser | null>(null);
  const [role, setRole] = useState<UserRole | undefined>(stored?.role);
  const [impersonatedTenantId, setImpersonated] = useState<string | null>(
    getActiveTenantId()
  );
  // Only bootstrap when there is a token worth resolving; otherwise the login
  // screen would sit behind a spinner on every cold visit.
  const [loading, setLoading] = useState<boolean>(Boolean(stored));
  const [error, setError] = useState<string | null>(null);

  const clearSession = useCallback(() => {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    setActiveTenantId(null);
    setImpersonated(null);
    setUser(null);
    setRole(undefined);
  }, []);

  const loadUser = useCallback(async () => {
    setLoading(true);
    try {
      const me = await fetchMe();
      setUser(me);
      setRole(me.role);
      setError(null);
    } catch {
      clearSession();
    } finally {
      setLoading(false);
    }
  }, [clearSession]);

  // A 401 from any request means the session is gone; drop it once, centrally,
  // rather than letting each screen invent its own recovery.
  useEffect(() => {
    setUnauthorizedHandler(clearSession);
    return () => setUnauthorizedHandler(null);
  }, [clearSession]);

  useEffect(() => {
    if (stored) void loadUser();
    // Runs once: `stored` is a snapshot of localStorage at mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signIn = useCallback(
    async (email: string, password: string) => {
      setError(null);
      try {
        const tokens = await loginRequest(email, password);
        localStorage.setItem(ACCESS_TOKEN_KEY, tokens.access_token);
        localStorage.setItem(REFRESH_TOKEN_KEY, tokens.refresh_token);
        setRole(decodeAccessToken(tokens.access_token)?.role);
        // A new session must not inherit the last operator's tenant selection.
        setActiveTenantId(null);
        setImpersonated(null);
        await loadUser();
      } catch (err: unknown) {
        const status = (err as { response?: { status?: number } })?.response?.status;
        setError(
          status === 401
            ? "Incorrect email or password."
            : "Unable to sign in. Please try again."
        );
        throw err;
      }
    },
    [loadUser]
  );

  const signOut = useCallback(() => {
    const refresh = localStorage.getItem(REFRESH_TOKEN_KEY);
    // Best effort: revoke server-side, but sign out locally regardless.
    if (refresh) void logoutRequest(refresh).catch(() => undefined);
    clearSession();
  }, [clearSession]);

  const inspectTenant = useCallback(
    (tenantId: string | null) => {
      // Guard in depth: a spa user has no tenant to switch to, and the API
      // would 403 the header anyway.
      if (role !== "super_admin") return;
      setActiveTenantId(tenantId);
      setImpersonated(tenantId);
    },
    [role]
  );

  const value = useMemo<AuthState>(
    () => ({
      user,
      role: user?.role ?? role,
      effectiveTenantId: impersonatedTenantId ?? user?.tenant_id ?? null,
      impersonatedTenantId,
      loading,
      error,
      signIn,
      signOut,
      inspectTenant,
    }),
    [user, role, impersonatedTenantId, loading, error, signIn, signOut, inspectTenant]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error("useAuth must be used inside an <AuthProvider>");
  }
  return context;
}
