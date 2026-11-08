import { useQuery } from '@tanstack/react-query';
import { atom } from 'jotai';
import { atomWithStorage } from 'jotai/utils';
import type { Effort, SovaApi, SovaResult } from '../shared/types';

export const sova = (window as unknown as { sova: SovaApi }).sova;

export class SovaError extends Error {
  constructor(public code: string, public hint?: string) { super(code); }
}

export async function callTool<T = any>(name: string, args: Record<string, unknown> = {}): Promise<SovaResult<T>> {
  const r = await sova.call<T>(name, args);
  if (!r.ok) throw new SovaError(r.code, r.hint);
  return r.result;
}

export function useTool<T = any>(name: string, args: Record<string, unknown> | null, refetchMs: number | false = false) {
  return useQuery({
    queryKey: [name, args], enabled: args !== null, retry: false, refetchInterval: refetchMs,
    queryFn: () => callTool<T>(name, args ?? {}),
  });
}

export type View = 'dashboard' | 'plans' | 'register' | 'timeline' | 'observe' | 'guide' | 'settings';
export const viewAtom = atom<View>('dashboard');
export const planAtom = atomWithStorage<string>('sova.plan', '');
export const panelAtom = atomWithStorage<boolean>('sova.agentPanel', true);
export const paletteAtom = atom(false);

export interface ChatItem {
  kind: 'user' | 'assistant' | 'tool' | 'error' | 'note' | 'turn';
  text?: string; id?: string; name?: string; input?: unknown; result?: string; isError?: boolean;
  durationMs?: number; tools?: number; at?: number;
}
export interface Conversation {
  id: string; title: string; sessionId?: string; items: ChatItem[];
  busy?: boolean; thinking?: boolean; retry?: string; turnStartedAt?: number;
  costUsd?: number; context?: { used: number; total?: number };
  model: string; effort: Effort; fast: boolean;
  pinned?: boolean; archived?: boolean; unread?: boolean; bookmarks?: number[]; queue?: string[]; forkPending?: boolean;
  createdAt: number; updatedAt: number;
}
export function blankConversation(title = '새 채팅', base?: Partial<Conversation>): Conversation {
  const now = Date.now();
  return { id: crypto.randomUUID(), title, items: [], model: '',
    effort: 'medium', fast: false, createdAt: now, updatedAt: now, ...base };
}
export const convSortAtom = atomWithStorage<'updated' | 'created'>('sova.convSort', 'updated');
export const archiveViewAtom = atom(false);
export const findAtom = atom<{ open: boolean; q: string; at: number }>({ open: false, q: '', at: 0 });
export const renameAtom = atom<string | null>(null);
export const convsAtom = atomWithStorage<Conversation[]>('sova.conversations', []);
export const activeConvAtom = atomWithStorage<string>('sova.activeConversation', '');
export const pendingConfirmAtom = atom<{ conv: string; id: string; name: string; input: Record<string, unknown> } | null>(null);

// Codes from sova_mcp / the link, explained for people new to SOVA.
export const CODE_TEXT: Record<string, string> = {
  mcp_url_not_set: 'SOVA MCP 주소가 아직 없습니다. 설정에서 주소를 넣어 주세요.',
  sso_login_required: 'AWS SSO 로그인이 필요합니다. "SSO 로그인"을 누르면 브라우저가 열립니다.',
  mcp_unreachable: 'SOVA MCP에 닿지 않습니다. tailnet 연결과 주소를 확인하세요. 결과는 미확인입니다.',
  upstream_401: '서버가 신원 증명을 받지 않았습니다. 이 계정이 MCP 신원으로 등록됐는지 확인하세요.',
  forbidden: '권한이 없거나 없는 대상입니다.',
  read_only_connection: '읽기 전용 연결입니다. 등록·계획 생성은 설정에서 "쓰기 허용"을 켜야 합니다.',
  plan_scoped_connection: '이 연결은 한 계획으로 범위가 고정돼 있습니다.',
  environment_not_connected: '이 환경의 관측 연결이 아직 없습니다. 미관측은 0이 아닙니다.',
  observability_not_connected: '관측 API가 아직 연결되지 않았습니다. 미관측은 0이 아닙니다.',
  security_not_connected: '보안 발견 API가 아직 연결되지 않았습니다.',
  upstream_unavailable: '조회 대상에 닿지 못했습니다. 결과 불명이며 다시 조회합니다.',
  user_declined: '확인 창에서 등록을 승인하지 않아 아무것도 등록하지 않았습니다.',
  confirmation_invalid: '확인 기간이 지났거나 입력이 바뀌었습니다. 미리보기부터 다시 하세요.',
  registration_reference_unavailable: '이 연결 참조를 찾지 못했거나 그 대상에 닿지 못했습니다. 목록에서 고르거나 운영자에게 확인하세요.',
  invalid_environment_reference: '이 테넌트에 없는 연결 참조입니다. 목록에서 고르세요.',
  environment_role_mismatch: '이 연결 참조는 고른 역할(출발지·목적지)에 쓸 수 없습니다.',
  environment_changed: '같은 이름의 환경이 다른 입력으로 이미 등록돼 있습니다. 다른 이름을 쓰세요.',
  workload_changed: '같은 앱이 다른 입력으로 이미 등록돼 있습니다. 기존 등록을 쓰거나 이름을 바꾸세요.',
  registration_exists: '이미 등록돼 있습니다. 아래 목록의 기존 등록을 쓰세요.',
  namespace_mismatch: '네임스페이스가 출발지 환경의 네임스페이스와 다릅니다.',
  invalid_release_path: '앱 경로·IaC 경로는 저장소 안의 상대 경로여야 합니다.',
  invalid_release_identity: '저장소·경로가 운영자가 승인한 배포와 맞지 않습니다. "운영자가 승인한 저장소"에서 고르세요.',
  missing_rollout_identity: '출발지에서 이 앱의 실제 배포를 찾지 못했습니다. 네임스페이스와 출발지 환경을 확인하세요.',
  evaluation_profile_unavailable: '이 평가 프로필은 쓸 수 없습니다. 목록에서 고르세요.',
  plan_conflict: '같은 입력의 계획이 이미 다른 상태로 있습니다. 이전 계획에서 확인하세요.',
  command_key_conflict: '같은 확인이 다른 입력에 쓰였습니다. 미리보기부터 다시 하세요.',
};
export const explain = (e: unknown) =>
  e instanceof SovaError ? CODE_TEXT[e.code] ?? e.hint ?? e.code : String((e as Error)?.message ?? e);

export const ago = (iso?: string | null) => {
  if (!iso) return '—';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 60 ? `${Math.round(s)}초 전` : s < 3600 ? `${Math.round(s / 60)}분 전` : `${Math.round(s / 3600)}시간 전`;
};
