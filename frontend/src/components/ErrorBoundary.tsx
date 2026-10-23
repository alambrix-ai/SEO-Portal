/**
 * Catches render errors so a bad page cannot white-screen the whole console.
 * Shows a friendly retry UI; stacks and PII stay out of the user-facing copy.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

import { reportClientError } from '@/lib/clientLog'

type Props = {
  children: ReactNode
  /** Distinguishes app-shell vs route boundaries in logs. */
  where?: string
  /** Optional compact layout (inside the page column). */
  variant?: 'page' | 'app'
}

type State = {
  failed: boolean
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    reportClientError(error, {
      where: this.props.where ?? 'error-boundary',
      route:
        typeof window !== 'undefined' ? window.location.pathname : undefined,
    })
    if (import.meta.env.DEV && info.componentStack) {
      console.error('[synaprank] component stack', info.componentStack)
    }
  }

  private retry = () => {
    this.setState({ failed: false })
  }

  render() {
    if (!this.state.failed) return this.props.children

    const page = this.props.variant !== 'app'
    return (
      <div
        className={page ? 'route-error' : 'app-error'}
        role="alert"
        data-testid="error-boundary"
      >
        <div className={page ? 'card elev-sm empty-state' : 'app-error-card'}>
          <div className="card-title">Something went wrong on this screen</div>
          <p className="card-body">
            The page could not be shown. Your other screens should still work —
            try again, or go back to Overview.
          </p>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <button type="button" className="btn btn-primary" onClick={this.retry}>
              Try again
            </button>
            <a className="btn btn-secondary" href="/dashboard">
              Back to Overview
            </a>
          </div>
        </div>
      </div>
    )
  }
}
