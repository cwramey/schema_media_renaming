import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@fontsource-variable/inter/index.css';
import '@fontsource-variable/jetbrains-mono/index.css';
import '@fontsource-variable/space-grotesk/index.css';
import './styles/index.css';
import { initTheme } from './lib/theme';
import { App } from './App';

initTheme();

async function boot() {
  // Dev-only API mock (`npm run dev:mock`); statically removed from production builds.
  if (import.meta.env.MODE === 'mock') {
    const { installMocks } = await import('./mocks/server');
    installMocks();
  }
  const root = document.getElementById('root');
  if (!root) throw new Error('#root missing');
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void boot();
