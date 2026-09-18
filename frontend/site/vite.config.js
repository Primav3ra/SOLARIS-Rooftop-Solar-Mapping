import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, '../..');

// Paths are resolved against this config's own location, never the process
// cwd. Same class of bug as the relative StaticFiles path that made the API
// work only when launched from the repo root.
export default defineConfig({
  plugins: [react()],

  // The eval and ML reports are imported at build time. This alias is what
  // makes the Validation and ML pages structurally incapable of drifting from
  // the committed results: there are no hand-copied numbers to go stale,
  // because the numbers are the artifacts.
  resolve: {
    alias: {
      '@reports': resolve(repoRoot, 'evals/reports'),
      '@mlreports': resolve(repoRoot, 'ml/reports'),
      '@artifacts': resolve(repoRoot, 'ml/artifacts'),
    },
  },

  server: {
    port: 5173,
    // Dev server proxies the API, so `npm run dev` and a local uvicorn work
    // together without CORS configuration.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },

  build: {
    outDir: resolve(repoRoot, 'src/solaris/api/static'),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: {
      output: {
        // Manual chunks so the WebGL intro is its own file. The content routes
        // must never download three.js -- it is ~700 kB and they have no use
        // for it. `npm run budget` asserts this in CI rather than trusting it.
        manualChunks(id) {
          if (id.includes('three') || id.includes('@react-three')) return 'intro-webgl';
          if (id.includes('maplibre-gl')) return 'map';
          if (id.includes('chart.js') || id.includes('react-chartjs')) return 'charts';
          if (id.includes('node_modules')) return 'vendor';
          return undefined;
        },
      },
    },
  },
});
