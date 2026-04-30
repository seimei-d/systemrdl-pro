// esbuild bundle for the viewer-core package. Outputs:
//
//   dist/viewer.js   — IIFE bundle exposing window.RdlViewer.mount(...)
//   dist/viewer.css  — extracted stylesheet
//
// IIFE keeps the consumer side trivial: a single <script src="…/viewer.js">
// gives both surfaces (VSCode webview, CLI browser SPA) the same API
// without bundler integration on their side.

import { build } from 'esbuild';
import { mkdirSync } from 'node:fs';

mkdirSync('dist', { recursive: true });

await build({
  entryPoints: ['src/index.tsx'],
  outfile: 'dist/viewer.js',
  bundle: true,
  format: 'iife',
  globalName: 'RdlViewer',
  platform: 'browser',
  target: ['es2022'],
  jsx: 'automatic',
  minify: true,
  sourcemap: false,
  loader: { '.css': 'css' },
  define: { 'process.env.NODE_ENV': '"production"' },
});

// Bundle CSS separately so consumers can load it via <link> instead of forcing
// runtime injection. esbuild emits dist/viewer.css from the imported CSS files.
await build({
  entryPoints: ['src/styles.css'],
  outfile: 'dist/viewer.css',
  bundle: true,
  loader: { '.css': 'css' },
  minify: true,
});

console.log('rdl-viewer-core: built dist/viewer.js + dist/viewer.css');
