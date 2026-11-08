// Models the person's own Codex (ChatGPT) login can use, read from Codex's local model cache
// (metadata only; no credential is read). The SOVA guide runs through that same login.
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type { ModelInfo } from '../shared/types';

const home = () => process.env.CODEX_HOME ?? join(homedir(), '.codex');

// Codex's recommended line-up ("Choosing a model": Astra, GPT-6.1 Sol, Luna). The local cache holds what this client
// build last fetched and can leave them out, so they are added when missing; the runtime still decides availability.
const RECOMMENDED: ModelInfo[] = [
  { slug: 'gpt-6-astra', label: 'GPT-6-Astra', description: '여러 단계·도구에 걸친 가장 어려운 작업', defaultEffort: 'low',
    efforts: (['low', 'medium', 'high', 'xhigh', 'max', 'ultra'] as const).map((id) => ({ id, note: '' })), fast: null, api: true },
  { slug: 'gpt-6.1-sol', label: 'GPT-6.1-Sol', description: 'Astra에 가까운 성능, 더 낮은 비용. 반복·장시간 작업', defaultEffort: 'medium',
    efforts: (['low', 'medium', 'high', 'xhigh', 'max'] as const).map((id) => ({ id, note: '' })), fast: null, api: true },
];

export function codexModels(): ModelInfo[] {
  let cached: ModelInfo[] = [];
  try {
    const { models } = JSON.parse(readFileSync(join(home(), 'models_cache.json'), 'utf8'));
    cached = (models as any[]).filter((m) => m.visibility === 'list').map((m) => ({
      slug: m.slug, label: m.display_name ?? m.slug, description: m.description ?? '',
      efforts: (m.supported_reasoning_levels ?? []).map((l: any) => ({ id: l.effort, note: l.description ?? '' })),
      defaultEffort: m.default_reasoning_level ?? 'medium', contextWindow: m.context_window,
      fast: (m.service_tiers ?? []).find((t: any) => t.id === 'priority') ?? null,
      api: m.supported_in_api !== false,
    }));
  } catch { /* no cache yet: the recommended models still show */ }
  return [...RECOMMENDED.filter((r) => !cached.some((m) => m.slug === r.slug)), ...cached];
}

export function codexDefaultModel(): string | null {
  try { return /^model\s*=\s*"([^"]+)"/m.exec(readFileSync(join(home(), 'config.toml'), 'utf8'))?.[1] ?? null; } catch { return null; }
}

/** Names of the person's own MCP servers, so the SOVA guide can switch them off (only SOVA tools). */
export function personalMcpServers(): string[] {
  try {
    const names = [...readFileSync(join(home(), 'config.toml'), 'utf8').matchAll(/^\[mcp_servers\.([A-Za-z0-9_-]+)\]/gm)].map((m) => m[1]);
    return [...new Set(names)].filter((n) => n !== 'sova');
  } catch { return []; }
}
