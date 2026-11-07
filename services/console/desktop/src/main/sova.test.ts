import { describe, expect, it } from 'vitest';
import { confirmRequest } from './sova';

const call = (name: string, args: Record<string, unknown>) =>
  Buffer.from(JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }));

describe('confirmRequest', () => {
  it('holds only the confirming call of a state-making tool', () => {
    expect(confirmRequest(call('sova_create_plan', { confirmation: 'tok', workload_id: 'w' }))?.name).toBe('sova_create_plan');
    expect(confirmRequest(call('sova_create_plan', { workload_id: 'w' }))).toBeNull(); // preview
    expect(confirmRequest(call('sova_get_plan', { plan_id: 'p', confirmation: 'x' }))).toBeNull();
    expect(confirmRequest(Buffer.from('not json'))).toBeNull();
  });
});
