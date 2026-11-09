// Stages the Codex runtime (OpenAI's native CLI behind @openai/codex-sdk) for embedding in the app,
// as the Codex desktop app does (Resources/codex-cli). Voice resources are left out.
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

const triple = { arm64: 'aarch64-apple-darwin', x64: 'x86_64-apple-darwin' }[process.arch];
const pkg = { arm64: '@openai/codex-darwin-arm64', x64: '@openai/codex-darwin-x64' }[process.arch];
const req = createRequire(join(process.cwd(), 'package.json'));
const vendor = join(dirname(req.resolve('@openai/codex/package.json')), '..', pkg.split('/')[1], 'vendor', triple);
const out = join(process.cwd(), 'build', 'codex-runtime');
if (!existsSync(join(vendor, 'bin', 'codex'))) throw new Error(`codex runtime not found for ${triple}`);
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
for (const part of ['bin', 'codex-path', 'codex-package.json']) cpSync(join(vendor, part), join(out, part), { recursive: true });
console.log(`staged ${triple} codex runtime → ${out}`);
