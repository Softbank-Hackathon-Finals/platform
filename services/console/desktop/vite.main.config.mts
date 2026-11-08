import { defineConfig } from 'vite';
// The Codex SDK is ESM-only; it is bundled into the CJS main and given the person's installed codex
// CLI path at runtime. Its import.meta.url (only used to resolve packages) becomes this file's URL.
export default defineConfig({ define: { 'import.meta.url': 'require("node:url").pathToFileURL(__filename).href' } });
