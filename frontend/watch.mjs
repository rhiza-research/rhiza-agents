import * as esbuild from 'esbuild';
import { watch } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const srcDir = resolve(__dirname, 'src');

const buildOptions = {
    entryPoints: ['src/app.ts'],
    bundle: true,
    outfile: process.env.ESBUILD_OUTFILE || '../src/rhiza_agents/static/app.js',
    format: 'esm',
    sourcemap: true,
    loader: { '.png': 'dataurl', '.svg': 'dataurl', '.gif': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl', '.ttf': 'dataurl', '.eot': 'dataurl' },
};

async function build() {
    try {
        await esbuild.build(buildOptions);
        console.log('[watch] build finished');
    } catch (e) {
        console.error('[watch] build failed');
    }
}

// Initial build
await build();
console.log('[watch] watching for changes...');

// Poll-based watcher: check mtimes every 500ms
import { readdirSync, statSync } from 'fs';

function getAllFiles(dir) {
    let files = [];
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = resolve(dir, entry.name);
        if (entry.isDirectory()) {
            files = files.concat(getAllFiles(full));
        } else if (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx')) {
            files.push(full);
        }
    }
    return files;
}

let lastMtimes = new Map();

function checkForChanges() {
    let changed = false;
    try {
        const files = getAllFiles(srcDir);
        for (const f of files) {
            const mtime = statSync(f).mtimeMs;
            if (lastMtimes.get(f) !== mtime) {
                if (lastMtimes.has(f)) {
                    console.log(`[watch] changed: ${f.replace(srcDir + '/', '')}`);
                    changed = true;
                }
                lastMtimes.set(f, mtime);
            }
        }
    } catch (e) {
        // Directory might be temporarily unavailable during writes
    }
    return changed;
}

// Prime the mtime cache
checkForChanges();

// Poll loop
setInterval(async () => {
    if (checkForChanges()) {
        await build();
    }
}, 500);
