import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { App } from './App'
import { AuthProvider } from './auth/AuthContext'
import { ConfirmProvider } from './components/Confirm'
import { ToastProvider } from './components/Toasts'

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

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <ConfirmProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ConfirmProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)
