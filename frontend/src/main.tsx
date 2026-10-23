import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import { AuthProvider } from './auth/AuthContext'
import { ConfirmProvider } from './components/Confirm'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ToastProvider } from './components/Toasts'
import { reportClientError } from './lib/clientLog'

import './styles/industry.css'
import './styles/app.css'
import './styles/enterprise.css'
import './styles/guided.css'
import './styles/activity.css'
import './styles/states.css'
import './styles/content.css'
import './styles/analysis.css'
import './styles/site-analysis.css'
import './styles/connect.css'
import './styles/verification.css'
/* Cross-cutting viewport fixes — must load after feature sheets. */
import './styles/responsive.css'

const container = document.getElementById('root')
if (!container) throw new Error('#root is missing from index.html')

// Chunk load failures after a redeploy (stale hashed assets) surface as
// unhandled promise rejections — log them so support can spot a hard refresh need.
window.addEventListener('unhandledrejection', (event) => {
  reportClientError(event.reason, { where: 'unhandledrejection' })
})

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary where="app-shell" variant="app">
      <BrowserRouter>
        <ToastProvider>
          <ConfirmProvider>
            <AuthProvider>
              <App />
            </AuthProvider>
          </ConfirmProvider>
        </ToastProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
)
