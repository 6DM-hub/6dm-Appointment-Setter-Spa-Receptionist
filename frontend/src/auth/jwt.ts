import type { UserRole } from "../api/client";

export interface AccessTokenClaims {
  sub: string;
  role?: UserRole;
  tenant_id?: string | null;
  exp?: number;
  type?: string;
}

/**
 * Read the payload of an access token without verifying it.
 *
 * Verification is the server's job — this only lets the shell paint the correct
 * navigation on first load instead of flashing the full menu while `/auth/me`
 * is in flight. Nothing here is trusted for authorization: a tampered `role`
 * claim would reveal a menu whose every endpoint answers 403.
 */
export function decodeAccessToken(token: string): AccessTokenClaims | null {
  const payload = token.split(".")[1];
  if (!payload) return null;
  try {
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(json) as AccessTokenClaims;
  } catch {
    return null;
  }
}

export function isExpired(claims: AccessTokenClaims | null): boolean {
  if (!claims?.exp) return false;
  return claims.exp * 1000 <= Date.now();
}
