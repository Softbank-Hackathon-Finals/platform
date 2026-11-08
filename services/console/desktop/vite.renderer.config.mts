import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
// The dev server must not reload the running app when a check build writes .vite/ or a package lands in out/.
export default defineConfig({ plugins: [react(), tailwindcss()], server: { watch: { ignored: ['**/.vite/**', '**/out/**', '**/build/**'] } } });
