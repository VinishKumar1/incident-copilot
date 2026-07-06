import React from 'react'
import ReactDOM from 'react-dom/client'
import { MsalProvider } from '@azure/msal-react'
import App from './App.jsx'
import './styles.css'
import { loadAuthConfig, msalInstance } from './authConfig.js'

async function bootstrap() {
  const root = ReactDOM.createRoot(document.getElementById('root'))

  try {
    const cfg = await loadAuthConfig()

    if (cfg.sso_enabled && msalInstance) {
      root.render(
        <React.StrictMode>
          <MsalProvider instance={msalInstance}>
            <App ssoEnabled={true} />
          </MsalProvider>
        </React.StrictMode>,
      )
    } else {
      root.render(<React.StrictMode><App ssoEnabled={false} /></React.StrictMode>)
    }
  } catch (e) {
    console.error('Bootstrap error:', e)
    root.render(<React.StrictMode><App ssoEnabled={false} /></React.StrictMode>)
  }
}

bootstrap()
