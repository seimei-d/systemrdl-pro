// Bundle the extension + copy the viewer-core React bundle into media/viewer/.
//
// vsce packages whatever's in the extension directory at build time, so the
// React assets must physically live inside this package — symlinks won't
// survive the .vsix tarball. We copy on every build so the .vsix always
// ships matching JS+CSS for the renderer.

import { build } from 'esbuild';
import { copyFileSync, mkdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const viewerDist = path.resolve(here, '../rdl-viewer-core/dist');
const targetDir = path.resolve(here, 'media/viewer');

if (!existsSync(path.join(viewerDist, 'viewer.js'))) {
  console.error(
    '\nrdl-viewer-core has not been built.\n' +
    '   Run: bun --filter @systemrdl-pro/viewer-core build\n',
  );
  process.exit(1);
}

mkdirSync(targetDir, { recursive: true });
for (const f of ['viewer.js', 'viewer.css']) {
  copyFileSync(path.join(viewerDist, f), path.join(targetDir, f));
}

await build({
  entryPoints: ['src/extension.ts'],
  outfile: 'out/extension.js',
  bundle: true,
  external: ['vscode'],
  format: 'cjs',
  platform: 'node',
  target: 'node18',
  minify: true,
});

console.log('vscode-systemrdl-pro: built out/extension.js + media/viewer/');
