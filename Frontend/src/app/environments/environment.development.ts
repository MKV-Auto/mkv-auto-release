const envServerIp =
  typeof globalThis !== 'undefined'
    ? (globalThis as any)?.process?.env?.['SERVER_IP']
    : undefined;

const host =
  envServerIp ||
  (typeof window !== 'undefined' ? window.location.hostname : 'localhost');

export const environment = {
  production: false,
  apiBase: `http://${host}:8000`,
  ffmpegHost: host,
  logLevel: (typeof globalThis !== 'undefined' && (globalThis as any)?.process?.env?.['MKVAUTO_DEBUG_LEVEL']) || 'DEBUG',
  logToBackend: ((typeof globalThis !== 'undefined' && (globalThis as any)?.process?.env?.['MKVAUTO_LOG_TO_BACKEND']) || 'false') === 'true',
};
