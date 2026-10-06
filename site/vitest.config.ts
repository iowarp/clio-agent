import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // The video package uses node:test and has its own dependency boundary.
    include: ['src/**/*.test.{ts,tsx,js,mjs}', 'scripts/**/*.test.{ts,js,mjs}'],
  },
});
