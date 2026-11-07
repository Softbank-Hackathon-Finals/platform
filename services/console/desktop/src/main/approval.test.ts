import { describe, expect, it, vi } from 'vitest';

vi.mock('./proof', () => ({ proofSource: () => async () => 'proof' }));
const { approval } = await import('./approval');
const base = { awsProfile: 'p', awsRegion: 'ap-northeast-2', approvalHost: '', approvalServerName: 'h.svc', approvalCert: '' } as any;

describe('approval', () => {
  it('refuses before any network call', async () => {
    expect((await approval(base, 'run-state', '../etc')).body.detail.code).toBe('invalid_plan_id');
    expect((await approval(base, 'run-state', 'plan-' + 'a'.repeat(32))).body.detail.code).toBe('approval_host_not_set');
    const set = { ...base, approvalHost: 'x', approvalCert: 'pem' };
    expect((await approval(set, 'approve-shift', 'plan-' + 'a'.repeat(32), 0)).body.detail.code).toBe('invalid_version');
  });
});
