import { msalInstance, loginRequest } from './authConfig.js'

async function getAccessToken() {
  if (!msalInstance || !loginRequest) return null
  const account = msalInstance.getActiveAccount() || msalInstance.getAllAccounts()[0]
  if (!account) return null
  try {
    const result = await msalInstance.acquireTokenSilent({ ...loginRequest, account })
    // idToken always has audience=clientId which our backend validates.
    return result.idToken || result.accessToken
  } catch (e) {
    console.warn('Silent token acquisition failed — redirecting to login:', e)
    // Token unrenewable silently — force a fresh login
    msalInstance.loginRedirect(loginRequest).catch(() => {})
    return null
  }
}

async function authHeaders() {
  const token = await getAccessToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

const json = async (r) => {
  if (r.status === 401) {
    // Backend rejected our token — it has expired. Trigger a fresh login.
    console.warn('API returned 401 — token expired, redirecting to login')
    if (msalInstance && loginRequest) {
      msalInstance.loginRedirect(loginRequest).catch(() => {})
    } else {
      window.location.reload()
    }
    throw new Error('Session expired — please sign in again')
  }
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r.json()
}

export const getStatus = async () =>
  fetch('/api/status', { headers: await authHeaders() }).then(json)

export const listIssues = async () =>
  fetch('/api/issues', { headers: await authHeaders() }).then(json)

export const listNamespaces = async () =>
  fetch('/api/namespaces', { headers: await authHeaders() }).then(json)

export const setNamespace = async (namespace) =>
  fetch('/api/namespace', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...await authHeaders() },
    body: JSON.stringify({ namespace }),
  }).then(json)

export const analyzeIssue = async (id, refresh = false) =>
  fetch(`/api/issues/${id}/analyze?refresh=${refresh}`, {
    method: 'POST',
    headers: await authHeaders(),
  }).then(json)

export const matchCode = async (id, refresh = false) =>
  fetch(`/api/issues/${id}/code-match?refresh=${refresh}`, {
    method: 'POST',
    headers: await authHeaders(),
  }).then(json)

export const fixIt = async (id) =>
  fetch(`/api/issues/${id}/fix-it`, {
    method: 'POST',
    headers: await authHeaders(),
  }).then(json)

export const searchKey = async (key, minutes = 120) =>
  fetch(`/api/search?key=${encodeURIComponent(key)}&minutes=${minutes}`, {
    headers: await authHeaders(),
  }).then(json)

export const searchSummary = async (key, minutes = 120) =>
  fetch(`/api/search/summary?key=${encodeURIComponent(key)}&minutes=${minutes}`, {
    headers: await authHeaders(),
  }).then(json)

export const createAdhocIssue = async (service, message, namespace = '') =>
  fetch('/api/issues/adhoc', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...await authHeaders() },
    body: JSON.stringify({ service, message, namespace }),
  }).then(json)

export const getNamespaceSummary = async (refresh = false) =>
  fetch(`/api/summary?refresh=${refresh}`, {
    headers: await authHeaders(),
  }).then(json)

export const getDashboard = async (hours = 24) =>
  fetch(`/api/analytics?hours=${hours}`, { headers: await authHeaders() }).then(json)

export const getVibeUsage = async () =>
  fetch('/api/analytics/vibe-usage', { headers: await authHeaders() }).then(json)

export const sendChat = async (issueId, messages) =>
  fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...await authHeaders() },
    body: JSON.stringify({ issue_id: issueId, messages }),
  }).then(json)
