import { PublicClientApplication } from '@azure/msal-browser'

// Singleton — shared by main.jsx (MsalProvider) and api.js (token acquisition).
export let msalInstance = null
export let loginRequest = null

export async function loadAuthConfig() {
  const res = await fetch('/auth/config')
  const cfg = await res.json()

  if (!cfg.sso_enabled) return { sso_enabled: false }

  const msalConfig = {
    auth: {
      clientId: cfg.client_id,
      authority: `https://login.microsoftonline.com/${cfg.tenant_id}`,
      redirectUri: window.location.origin,
      postLogoutRedirectUri: window.location.origin,
    },
    cache: {
      // localStorage persists across the full-page redirect so MSAL can
      // read back the account after Azure redirects the browser back here.
      cacheLocation: 'localStorage',
      storeAuthStateInCookie: true,
    },
  }

  loginRequest = { scopes: ['openid', 'profile', 'email'] }

  msalInstance = new PublicClientApplication(msalConfig)
  await msalInstance.initialize()

  // Process the auth code that Azure appended to the redirect URL.
  const redirectResult = await msalInstance.handleRedirectPromise()

  // After a redirect login, MSAL returns the account in the result.
  // We must set it as the active account so AuthenticatedTemplate renders.
  if (redirectResult?.account) {
    msalInstance.setActiveAccount(redirectResult.account)
  } else {
    // On subsequent page loads (no redirect), pick up the existing account.
    const accounts = msalInstance.getAllAccounts()
    if (accounts.length > 0) {
      msalInstance.setActiveAccount(accounts[0])
    }
  }

  return { sso_enabled: true }
}
