// The Codex runtime embedded in the desktop app and its ChatGPT sign-in (Electron only; the web console
// server resolves its Linux runtime itself and signs in with the platform-held API key).
import { app } from 'electron';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

/** The Codex runtime embedded in the app (Resources/codex-runtime), staged from @openai/codex's native package. */
export function codexRuntime() {
  const triple = process.arch === 'arm64' ? 'aarch64-apple-darwin' : 'x86_64-apple-darwin';
  const root = app.isPackaged ? join(process.resourcesPath, 'codex-runtime')
    : join(app.getAppPath(), 'node_modules', '@openai', process.arch === 'arm64' ? 'codex-darwin-arm64' : 'codex-darwin-x64', 'vendor', triple);
  const bin = join(root, 'bin', 'codex');
  if (!existsSync(bin)) throw new Error('codex_runtime_missing');
  return { bin, path: join(root, 'codex-path') };
}

/** ChatGPT sign-in state and sign-in through the embedded runtime (Codex app onboarding). */
export function codexAuth(login = false): Promise<{ ok: boolean; message: string }> {
  return new Promise((resolve) => {
    let rt: ReturnType<typeof codexRuntime>;
    try { rt = codexRuntime(); } catch { resolve({ ok: false, message: '내장 Codex 런타임을 찾지 못했습니다.' }); return; }
    const child = spawn(rt.bin, login ? ['login'] : ['login', 'status'], { env: { ...process.env, PATH: `${rt.path}:${process.env.PATH ?? ''}` } });
    let out = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { out += d; });
    child.on('exit', (code) => resolve({ ok: code === 0 && !/not logged in/i.test(out), message: out.trim().split('\n').at(-1) ?? '' }));
  });
}
