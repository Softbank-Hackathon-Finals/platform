// SOVA MCP identity proof: the same STS GetCallerIdentity presign the T10 hosts verify
// (bravo_platform_events.registration_sso), signed for the distinct `sova-mcp` audience.
// The proof lives 60 s; it is minted in memory and never logged or written.
import { Sha256 } from '@aws-crypto/sha256-js';
import { fromSSO } from '@aws-sdk/credential-providers';
import { SignatureV4 } from '@smithy/signature-v4';

export const PREFIX = 'bravo-registration-v1.';
export const AUDIENCE_HEADER = 'x-bravo-registration-id';
export const AUDIENCE = 'sova-mcp';

type Creds = { accessKeyId: string; secretAccessKey: string; sessionToken?: string };

const rfc3986 = (v: string) =>
  encodeURIComponent(v).replace(/[!'()*]/g, (c) => '%' + c.charCodeAt(0).toString(16).toUpperCase());

export async function mintProof(opts: { region: string; credentials: () => Promise<Creds>; now?: Date; audience?: string }) {
  if (!/^[a-z]{2}-[a-z]+-\d$/.test(opts.region)) throw new Error('invalid_region');
  const creds = await opts.credentials();
  if (!creds.sessionToken || !creds.accessKeyId.startsWith('ASIA')) throw new Error('sso_session_credentials_required');
  const hostname = `sts.${opts.region}.amazonaws.com`;
  const signer = new SignatureV4({ service: 'sts', region: opts.region, credentials: creds, sha256: Sha256 });
  const signed = await signer.presign(
    { method: 'GET', protocol: 'https:', hostname, path: '/',
      query: { Action: 'GetCallerIdentity', Version: '2011-06-15' },
      headers: { host: hostname, [AUDIENCE_HEADER]: opts.audience ?? AUDIENCE } },
    { expiresIn: 60, signingDate: opts.now },
  );
  const query = Object.entries(signed.query ?? {})
    .map(([k, v]) => `${rfc3986(k)}=${rfc3986(String(v))}`)
    .join('&');
  return PREFIX + Buffer.from(`https://${hostname}/?${query}`).toString('base64url');
}

/** Reuses one proof for 40 s of its 60 s life; the server caches it for the same window. */
export function proofSource(profile: string, region: string, audience = AUDIENCE) {
  let cached: { token: string; at: number } | null = null;
  return async () => {
    if (cached && Date.now() - cached.at < 40_000) return cached.token;
    const token = await mintProof({ region, credentials: fromSSO({ profile }), audience });
    cached = { token, at: Date.now() };
    return token;
  };
}
