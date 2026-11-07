import { app } from 'electron';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Settings } from '../shared/types';

const DEFAULTS: Settings = {
  mcpUrl: '', awsProfile: 'bravo-operator', awsRegion: 'ap-northeast-2', readOnly: true, planScope: '',
  model: '', devToken: '', onboarded: false, gcpProject: '', tailnet: '',
};
const file = () => join(app.getPath('userData'), 'settings.json');

export function loadSettings(): Settings {
  try { return { ...DEFAULTS, ...JSON.parse(readFileSync(file(), 'utf8')) }; } catch { return { ...DEFAULTS }; }
}

export function saveSettings(patch: Partial<Settings>): Settings {
  const next = { ...loadSettings(), ...patch };
  if (next.mcpUrl && !/^https?:\/\/[^/]+\/mcp$/.test(next.mcpUrl)) throw new Error('mcp_url_must_end_with_/mcp');
  if (next.planScope && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(next.planScope)) throw new Error('invalid_plan_scope');
  writeFileSync(file(), JSON.stringify(next, null, 2), { mode: 0o600 });
  return next;
}

export const isLoopback = (url: string) => /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\//.test(url);
