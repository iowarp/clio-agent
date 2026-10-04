import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  retries: 0,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:4389', trace: 'retain-on-failure' },
  webServer: {
    command: 'pnpm preview --host 127.0.0.1 --port 4389 --ignore-lock',
    url: 'http://127.0.0.1:4389',
    reuseExistingServer: false,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
