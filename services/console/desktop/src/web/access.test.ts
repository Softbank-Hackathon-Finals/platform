import { generateKeyPairSync, sign } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { AccessVerifier } from './access';

const TEAM = 'bravo.cloudflareaccess.com', AUD = 'a'.repeat(64);
const team = generateKeyPairSync('rsa', { modulusLength: 2048 }), other = generateKeyPairSync('rsa', { modulusLength: 2048 });
const jwks = { keys: [{ ...team.publicKey.export({ format: 'jwk' }), kid: 'k1', alg: 'RS256' }] };
const b64 = (o: object) => Buffer.from(JSON.stringify(o)).toString('base64url');
const now = () => Math.floor(Date.now() / 1000);
function token(claims: Record<string, unknown> = {}, key = team.privateKey, kid = 'k1') {
  const body = { iss: `https://${TEAM}`, aud: [AUD], iat: now(), exp: now() + 600, email: 'Person@Example.org', ...claims };
  const head = `${b64({ alg: 'RS256', kid })}.${b64(body)}`;
  return `${head}.${sign('RSA-SHA256', Buffer.from(head), key).toString('base64url')}`;
}
let fetches = 0;
const verifier = () => new AccessVerifier({ team: TEAM, aud: AUD, fetchFn: (async () => { fetches += 1; return new Response(JSON.stringify(jwks)); }) as typeof fetch });

describe('AccessVerifier', () => {
  it('accepts the team token for this app and lowercases the email', async () => {
    expect((await verifier().verify(token()))?.email).toBe('person@example.org');
  });

  it('refuses another app, issuer, an expired or forged token and junk', async () => {
    const v = verifier();
    for (const bad of [token({ aud: ['other'] }), token({ iss: 'https://evil.cloudflareaccess.com' }), token({ exp: now() - 5 }),
      token({}, other.privateKey), token({ email: 'not-an-email' }), 'a.b', '', undefined]) {
      expect(await v.verify(bad as string)).toBeNull();
    }
  });

  it('does not refetch keys for every unknown key id', async () => {
    const v = verifier();
    await v.verify(token());
    const before = fetches;
    for (let i = 0; i < 5; i += 1) await v.verify(token({}, team.privateKey, `unknown-${i}`));
    expect(fetches - before).toBeLessThanOrEqual(1);
  });
});
