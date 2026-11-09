// Cloudflare Access identity for the web console. Every request through the tunnel carries the person's
// Access token (Cf-Access-Jwt-Assertion). It is verified here against the team's published keys, issuer and
// the Access application's audience tag; the SOVA MCP verifies the same token again on every call.
import { createPublicKey, verify, type KeyObject } from 'node:crypto';

export interface AccessConfig { team: string; aud: string; fetchFn?: typeof fetch; now?: () => number }
export interface AccessIdentity { email: string; exp: number; token: string }

const b64 = (s: string) => Buffer.from(s, 'base64url');

export class AccessVerifier {
  private keys = new Map<string, KeyObject>();
  private fetchedAt = 0;

  constructor(private cfg: AccessConfig) {}

  private async refresh(force = false) {
    const now = (this.cfg.now ?? Date.now)();
    if (!force && now - this.fetchedAt < 3_600_000 && this.keys.size) return;
    if (force && now - this.fetchedAt < 30_000) return; // an unknown kid does not turn into a fetch storm
    const r = await (this.cfg.fetchFn ?? fetch)(`https://${this.cfg.team}/cdn-cgi/access/certs`, { signal: AbortSignal.timeout(5_000) });
    if (!r.ok) throw new Error('access_keys_unavailable');
    const { keys } = (await r.json()) as { keys: (JsonWebKey & { kid: string })[] };
    this.keys = new Map(keys.filter((k) => k.kty === 'RSA').map((k) => [k.kid, createPublicKey({ key: k as any, format: 'jwk' })]));
    this.fetchedAt = now;
  }

  /** The verified identity, or null for anything malformed, unsigned by the team, for another app or expired. */
  async verify(token: string | undefined | null): Promise<AccessIdentity | null> {
    if (!token || token.length > 8192) return null;
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    let header: any, claims: any;
    try { header = JSON.parse(b64(parts[0]).toString('utf8')); claims = JSON.parse(b64(parts[1]).toString('utf8')); } catch { return null; }
    if (header.alg !== 'RS256' || typeof header.kid !== 'string') return null;
    await this.refresh();
    if (!this.keys.has(header.kid)) await this.refresh(true);
    const key = this.keys.get(header.kid);
    if (!key || !verify('RSA-SHA256', Buffer.from(`${parts[0]}.${parts[1]}`), key, b64(parts[2]))) return null;
    const now = Math.floor((this.cfg.now ?? Date.now)() / 1000);
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (claims.iss !== `https://${this.cfg.team}` || !aud.includes(this.cfg.aud)) return null;
    if (typeof claims.exp !== 'number' || claims.exp <= now || (typeof claims.nbf === 'number' && claims.nbf > now + 60)) return null;
    if (typeof claims.email !== 'string' || !/^[^@\s]+@[^@\s]+$/.test(claims.email)) return null;
    return { email: claims.email.toLowerCase(), exp: claims.exp, token };
  }
}
