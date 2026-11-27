// Runs scripts/brand.py (repo root) with the backend virtualenv's Python (Pillow lives there).
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const candidates = [
  resolve(root, 'backend', '.venv', 'Scripts', 'python.exe'),
  resolve(root, 'backend', '.venv', 'bin', 'python'),
];
const python = candidates.find((p) => existsSync(p)) ?? (process.platform === 'win32' ? 'python' : 'python3');
const result = spawnSync(python, [resolve(root, 'scripts', 'brand.py'), ...process.argv.slice(2)], {
  stdio: 'inherit',
  cwd: root,
});
process.exit(result.status ?? 1);
