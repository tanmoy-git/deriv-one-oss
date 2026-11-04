import {defineConfig, mergeConfig} from 'vitest/config';
import viteConfig from './vite.config';

// Reuses the app's Vite config so tests resolve the same @/ aliases.
export default defineConfig((env) =>
  mergeConfig(viteConfig(env), {
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      css: false,
    },
  }),
);
