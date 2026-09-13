import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';
import { fileURLToPath } from 'node:url';
const local = (path: string) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  root: local('./mounted'),
  publicDir: local('./public'),
  plugins: [react()],
  css: { postcss: { plugins: [tailwindcss()] } },
  resolve: {
    alias: {
      '@': local('./'),
      'next/link': local('./mounted/link.tsx'),
      'next/image': local('./mounted/image.tsx'),
    },
  },
  build: { outDir: local('./dist-mounted'), emptyOutDir: true },
});
