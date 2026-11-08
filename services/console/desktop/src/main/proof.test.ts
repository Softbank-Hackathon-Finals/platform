import { describe, expect, it } from 'vitest';
import { AUDIENCE_HEADER, PREFIX, mintProof } from './proof';

// Mirrors SsoRegistrationVerifier._parse (platform-events/registration_sso.py).
describe('mintProof', () => {
  it('produces exactly the query the T10 verifier accepts', async () => {
    const token = await mintProof({
      region: 'ap-northeast-2', now: new Date('2026-11-07T13:00:00Z'),
      credentials: async () => ({ accessKeyId: 'ASIATESTKEY', secretAccessKey: 's', sessionToken: 'tok+/=' }),
    });
    expect(token.startsWith(PREFIX)).toBe(true);
    const url = new URL(Buffer.from(token.slice(PREFIX.length), 'base64url').toString('ascii'));
    expect(url.origin + url.pathname).toBe('https://sts.ap-northeast-2.amazonaws.com/');
    const q = Object.fromEntries(url.searchParams);
    expect(Object.keys(q).sort()).toEqual(['Action', 'Version', 'X-Amz-Algorithm', 'X-Amz-Credential', 'X-Amz-Date',
      'X-Amz-Expires', 'X-Amz-Security-Token', 'X-Amz-Signature', 'X-Amz-SignedHeaders']);
    expect(q['X-Amz-Expires']).toBe('60');
    expect(q['X-Amz-SignedHeaders']).toBe('host;' + AUDIENCE_HEADER);
    expect(q['X-Amz-Credential']).toBe('ASIATESTKEY/20261107/ap-northeast-2/sts/aws4_request');
    expect(q['X-Amz-Security-Token']).toBe('tok+/=');
    expect(q['X-Amz-Signature']).toMatch(/^[a-f0-9]{64}$/);
  });

  it('refuses long-term keys', async () => {
    await expect(mintProof({ region: 'ap-northeast-2',
      credentials: async () => ({ accessKeyId: 'AKIAEXAMPLE', secretAccessKey: 's' }) })).rejects.toThrow('sso_session');
  });
});
