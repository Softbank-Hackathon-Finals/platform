// Contract between main, preload and renderer. Result mirrors sova_mcp.server.Result.
export type Status = 'OK' | 'ACCEPTED' | 'RUNNING' | 'PASSED' | 'FAILED' | 'BLOCKED' | 'UNKNOWN' | 'NEEDS_CONFIRMATION';
export interface NextAction { action: string; tool?: string; requires?: string; console_url?: string; why?: string }
export interface SovaResult<T = any> {
  status: Status; summary: string; data: T; next_actions: NextAction[]; poll_after_ms: number | null;
  cursor: string | null; console_url: string | null; confirmation: string | null; observed_at: string; source: 'live';
}
export type CallOutcome<T = any> = { ok: true; result: SovaResult<T> } | { ok: false; code: string; hint?: string };

export interface Settings {
  mcpUrl: string;        // e.g. http://sova-mcp.<tailnet>.ts.net:443/mcp
  awsProfile: string;    // AWS SSO profile used to sign the MCP proof
  awsRegion: string;
  readOnly: boolean;     // read_only=false exposes register/plan tools (two-step confirmed)
  planScope: string;     // optional ?plan= scope
  model: string;
  devToken: string;      // only for a loopback MCP (local test server)
  onboarded: boolean;
  gcpProject: string;     // console entry (Google account picks itself in the browser)
  tailnet: string;        // Tailscale admin tailnet name for the on-prem destination
}
export interface LinkStatus { connected: boolean; url: string; readOnly: boolean; code?: string; tools: string[]; checkedAt: string }

export type AgentEvent =
  | { conv: string; type: 'text'; delta: string }
  | { conv: string; type: 'thinking'; on: boolean }
  | { conv: string; type: 'retry'; attempt: number; max: number }
  | { conv: string; type: 'usage'; used: number; total?: number }
  | { conv: string; type: 'tool'; id: string; name: string; input: unknown }
  | { conv: string; type: 'tool_result'; id: string; text: string; isError: boolean }
  | { conv: string; type: 'confirm'; id: string; name: string; input: Record<string, unknown> }
  | { conv: string; type: 'done'; sessionId?: string; costUsd?: number; turns?: number; durationMs?: number; total?: number }
  | { conv: string; type: 'error'; message: string };

export type Effort = 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'ultra';
// Codex labels (composer.mode.local.reasoning.*): Light, Medium, High, Extra High, Max, Ultra.
export const EFFORT_LABEL: Record<Effort, string> = { low: 'Light', medium: 'Medium', high: 'High', xhigh: 'Extra High', max: 'Max', ultra: 'Ultra' };
export const EFFORT_ORDER: Effort[] = ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
export interface ModelInfo {
  slug: string; label: string; description: string; efforts: { id: Effort; note: string }[]; defaultEffort: Effort;
  contextWindow?: number; fast: { name: string; description: string } | null;
}
export interface TurnOptions { sessionId?: string; model: string; effort: Effort; fast: boolean; fork?: boolean }
export interface PromptInfo { name: string; title?: string; description?: string; args: { name: string; required?: boolean }[] }

export const READ_TOOLS = ['sova_get_context', 'sova_next_steps', 'sova_list_plans', 'sova_request_approval', 'sova_get_plan',
  'sova_get_progress', 'sova_compare', 'sova_get_execution', 'sova_get_observability', 'sova_get_security'] as const;
export const WRITE_TOOLS = ['sova_register_environment', 'sova_register_workload', 'sova_create_plan'] as const;

export interface SovaApi {
  call<T = any>(name: string, args?: Record<string, unknown>): Promise<CallOutcome<T>>;
  status(): Promise<LinkStatus>;
  resource(uri: string): Promise<string | null>;
  settings(): Promise<Settings>;
  saveSettings(patch: Partial<Settings>): Promise<Settings>;
  ssoLogin(): Promise<{ ok: boolean; message: string }>;
  agentSend(conv: string, text: string, opts: TurnOptions): Promise<void>;
  prompts(): Promise<PromptInfo[]>;
  models(): Promise<{ models: ModelInfo[]; defaultModel: string | null }>;
  agentAuth(login: boolean): Promise<{ ok: boolean; message: string }>;
  awsProfile(): Promise<{ startUrl?: string; accountId?: string; roleName?: string; region?: string }>;
  prompt(name: string, args: Record<string, string>): Promise<string | null>;
  agentStop(conv: string): Promise<void>;
  agentConfirm(id: string, allow: boolean): Promise<void>;
  onAgent(cb: (e: AgentEvent) => void): () => void;
  onDeepLink(cb: (url: string) => void): () => void;
}
