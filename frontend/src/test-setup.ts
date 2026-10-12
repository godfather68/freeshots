import '@testing-library/jest-dom/vitest'
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'
import { resetStores } from './lib/storage'

afterEach(() => {
  cleanup()
  localStorage.clear()
  resetStores()
  window.location.hash = ''
})
