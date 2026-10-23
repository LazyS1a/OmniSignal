import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
 plugins: [react()],
 base: '/workbench/',
 server: { proxy: { '/workbench/api': 'http://127.0.0.1:8010' } },
 build: { outDir: 'dist', sourcemap: false },
});
