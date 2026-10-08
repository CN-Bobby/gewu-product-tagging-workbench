import {defineConfig} from 'vite';
import {readFileSync} from 'node:fs';

const version = JSON.parse(readFileSync(new URL('../VERSION.json', import.meta.url), 'utf8')).version;
const devPort = 5174;
const expectedOrigins = new Set([
  `http://127.0.0.1:${devPort}`,
  `http://localhost:${devPort}`,
]);

function getDevApiTarget() {
  const configured = process.env.GEWU_DEV_API_URL || 'http://127.0.0.1:8896';
  const url = new URL(configured);
  const localHosts = new Set(['127.0.0.1', 'localhost', '[::1]']);
  if (url.protocol !== 'http:' || !localHosts.has(url.hostname) || url.port !== '8896' || url.pathname !== '/') {
    throw new Error('GEWU_DEV_API_URL must be a loopback HTTP URL on DEV port 8896.');
  }
  return url.origin;
}

const apiTarget = getDevApiTarget();

export default defineConfig({
  define: {__APP_VERSION__: JSON.stringify(version)},
  server: {
    host: '127.0.0.1',
    port: devPort,
    strictPort: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false,
        configure(proxy) {
          proxy.on('proxyReq', (proxyRequest, request, response) => {
            const origin = request.headers.origin;
            const host = request.headers.host;
            const localHost = host === `127.0.0.1:${devPort}` || host === `localhost:${devPort}`;
            if (!localHost || (origin && !expectedOrigins.has(origin))) {
              if (!response.headersSent) {
                response.writeHead(403, {'content-type': 'text/plain; charset=utf-8'});
                response.end('Rejected non-local DEV API proxy request.');
              }
              proxyRequest.destroy();
            }
          });
        },
      },
    },
  },
});
