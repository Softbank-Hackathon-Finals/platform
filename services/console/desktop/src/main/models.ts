// Models the person's own Codex (ChatGPT) login can use, read from Codex's local model cache
// (metadata only; no credential is read). The SOVA guide runs through that same login.
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import type { ModelInfo } from '../shared/types';

const home = () => process.env.CODEX_HOME ?? join(homedir(), '.codex');

export function codexModels(): ModelInfo[] {
  try {
    const { models } = JSON.parse(readFileSync(join(home(), 'models_cache.json'), 'utf8'));
    return (models as any[]).filter((m) => m.visibility === 'list').map((m) => ({
      slug: m.slug, label: m.display_name ?? m.slug, description: m.description ?? '',
      efforts: (m.supported_reasoning_levels ?? []).map((l: any) => ({ id: l.effort, note: l.description ?? '' })),
      defaultEffort: m.default_reasoning_level ?? 'medium', contextWindow: m.context_window,
      fast: (m.service_tiers ?? []).find((t: any) => t.id === 'priority') ?? null,
    }));
  } catch { return []; }
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
