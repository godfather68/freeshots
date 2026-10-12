/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base './' + hash routing: assets and data/*.json resolve relative to the page, so the same build works
// at https://user.github.io/repo/, on a custom domain, or from a local folder. Override with VITE_BASE.
export default defineConfig({
  base: process.env.VITE_BASE ?? './',
  plugins: [react()],
  build: { sourcemap: false, target: 'es2022' },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
  },
})
