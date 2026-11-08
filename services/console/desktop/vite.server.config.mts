// The web console server as one CommonJS file: every dependency bundled, only Node built-ins outside.
import { defineConfig } from 'vite';
export default defineConfig({
  build: { ssr: 'src/web/server.ts', outDir: 'dist/server', emptyOutDir: true, target: 'node22', minify: false,
    rollupOptions: { output: { format: 'cjs', entryFileNames: 'server.cjs' } } },
  ssr: { noExternal: true },
});
