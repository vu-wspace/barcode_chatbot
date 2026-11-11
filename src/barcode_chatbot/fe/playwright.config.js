// E2E: npm ci && npx playwright install chromium && npx playwright test
const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: 'e2e',
  timeout: 30000,
  retries: 0,
  use: {
    baseURL: 'http://127.0.0.1:4173', ...devices['Desktop Chrome'],
    permissions: ['camera'],  // Chromium's fake camera (a test pattern); the page's decoder is faked per test
    launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] },
  },
  webServer: { command: 'node e2e/server.js', url: 'http://127.0.0.1:4173/', reuseExistingServer: false },
  workers: 1, // one fake server, its AI script is shared state
});
