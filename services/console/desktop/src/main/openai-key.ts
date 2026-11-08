// The person's OpenAI API key for the guide (Settings › Guide › OpenAI API key). Encrypted with the
// OS keychain (Electron safeStorage) in userData; the renderer only ever sees "configured" and the last 4.
import { app, safeStorage } from 'electron';
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const file = () => join(app.getPath('userData'), 'openai-key.bin');
const SHAPE = /^sk-[A-Za-z0-9_-]{20,300}$/;

export function readKey(): string | null {
  try { return existsSync(file()) ? safeStorage.decryptString(readFileSync(file())) : null; } catch { return null; }
}

export function keyStatus() {
  const k = readKey();
  return { configured: !!k, last4: k ? k.slice(-4) : null, encrypted: safeStorage.isEncryptionAvailable() };
}

/** Saves after the OpenAI API accepts it (GET /v1/models: no usage cost). */
export async function saveKey(key: string): Promise<{ ok: boolean; message: string; models?: string[] }> {
  key = key.trim();
  if (!SHAPE.test(key)) return { ok: false, message: 'OpenAI API 키 형식(sk-…)이 아닙니다.' };
  if (!safeStorage.isEncryptionAvailable()) return { ok: false, message: '키체인 암호화를 쓸 수 없어 저장하지 않았습니다.' };
  const check = await listModels(key);
  if (!check.ok) return check;
  writeFileSync(file(), safeStorage.encryptString(key), { mode: 0o600 });
  return { ok: true, message: `확인됨 · API 모델 ${check.models!.length}개`, models: check.models };
}

export function clearKey() { rmSync(file(), { force: true }); }

export async function listModels(key = readKey()): Promise<{ ok: boolean; message: string; models?: string[] }> {
  if (!key) return { ok: false, message: 'API 키가 없습니다.' };
  try {
    const r = await fetch('https://api.openai.com/v1/models', { headers: { Authorization: `Bearer ${key}` }, signal: AbortSignal.timeout(10_000) });
    if (r.status === 401) return { ok: false, message: 'OpenAI가 키를 거부했습니다(401).' };
    if (!r.ok) return { ok: false, message: `OpenAI 응답 ${r.status}` };
    const ids: string[] = ((await r.json()).data ?? []).map((m: { id: string }) => m.id);
    return { ok: true, message: 'ok', models: ids };
  } catch { return { ok: false, message: 'api.openai.com에 닿지 못했습니다. 네트워크를 확인하세요.' }; }
}
