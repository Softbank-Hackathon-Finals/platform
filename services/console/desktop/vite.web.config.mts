// The desktop renderer built for the browser (sova.mangowhois.ai) with the web window.sova shim.
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
import { defineConfig } from 'vite';
export default defineConfig({
  root: resolve(__dirname, 'web'), base: '/', plugins: [react(), tailwindcss()],
  build: { outDir: resolve(__dirname, 'dist/web'), emptyOutDir: true },
});
