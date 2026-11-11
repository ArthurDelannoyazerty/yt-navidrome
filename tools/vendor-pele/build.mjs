import {build} from 'esbuild';
import {copyFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));
const assets = new URL('../../src/static/', import.meta.url);
await build({
  absWorkingDir: here,
  entryPoints: ['entry.js'],
  outfile: fileURLToPath(new URL('pele-flowchart.js', assets)),
  bundle: true,
  minify: true,
  format: 'iife',
  globalName: 'IngestorPele',
  target: ['es2020'],
  legalComments: 'inline',
  banner: {js: '/*! Pele 1.13.0 | MIT | https://github.com/obsidianmd/pele | See pele-LICENSE.txt */'},
});
await copyFile(new URL('node_modules/pele/LICENSE', import.meta.url), new URL('pele-LICENSE.txt', assets));
