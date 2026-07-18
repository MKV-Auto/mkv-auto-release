// Production environment - uses relative paths for Docker container deployment
// NGINX reverse proxy routes /api/* to backend on port 8000

const host =
  (typeof window !== 'undefined' ? window.location.hostname : 'localhost');

export const environment = {
  production: true,
  apiBase: '/api',  // Relative path - NGINX proxies to backend
  ffmpegHost: host,
  logLevel: (typeof globalThis !== 'undefined' && (globalThis as any)?.process?.env?.['MKVAUTO_DEBUG_LEVEL']) || 'INFO',
  logToBackend: ((typeof globalThis !== 'undefined' && (globalThis as any)?.process?.env?.['MKVAUTO_LOG_TO_BACKEND']) || 'false') === 'true',
};
