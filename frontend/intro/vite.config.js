import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

// Builds a single JS entry into the FastAPI-served static assets.
// Output is referenced from src/solaris/api/static/index.html.
//
// outDir is resolved against this config's own location rather than the
// process cwd -- the same class of bug as the relative StaticFiles path that
// meant the server only worked when launched from the repo root.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: resolve(here, '../../src/solaris/api/static/intro-build'),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      input: resolve(here, 'src/intro-entry.jsx'),
      output: {
        entryFileNames: 'intro.bundle.js',
        assetFileNames: 'assets/[name][extname]'
      }
    }
  }
});
