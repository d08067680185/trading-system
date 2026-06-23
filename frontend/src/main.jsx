import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// Apply saved theme before first paint (prevents flash)
;(function () {
  const t = localStorage.getItem('theme')
  if (t === 'light') document.documentElement.setAttribute('data-theme', 'light')
})()

class ErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null } }
  static getDerivedStateFromError(e) { return { error: e } }
  render() {
    if (this.state.error) return (
      <div style={{ padding: 40, color: '#ff3c5c', fontFamily: 'monospace', whiteSpace: 'pre-wrap', background: '#0a0a0f', minHeight: '100vh' }}>
        <b>Runtime Error:</b>{'\n'}{this.state.error?.stack || String(this.state.error)}
      </div>
    )
    return this.props.children
  }
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>
)
