import { beforeEach, describe, expect, it, vi } from 'vitest'

const loginRedirect = vi.fn(() => Promise.resolve())
const acquireTokenSilent = vi.fn()
const getActiveAccount = vi.fn()
const getAllAccounts = vi.fn()

vi.mock('./authConfig.js', () => ({
  msalInstance: {
    acquireTokenSilent,
    getActiveAccount,
    getAllAccounts,
    loginRedirect,
  },
  loginRequest: { scopes: ['openid'] },
}))

const makeResponse = ({ ok = true, status = 200, statusText = 'OK', jsonData = {} } = {}) => ({
  ok,
  status,
  statusText,
  json: vi.fn().mockResolvedValue(jsonData),
})

describe('api helpers', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    loginRedirect.mockResolvedValue(undefined)
    getActiveAccount.mockReturnValue({ username: 'user@example.com' })
    getAllAccounts.mockReturnValue([])
    acquireTokenSilent.mockResolvedValue({ idToken: 'id-token' })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(makeResponse({ jsonData: { ok: true } })))
  })

  it('calls every exported API helper with the expected request', async () => {
    const api = await import('./api.js')

    const calls = [
      () => api.getStatus(),
      () => api.listIssues(),
      () => api.listNamespaces(),
      () => api.setNamespace('iom-preprod'),
      () => api.analyzeIssue('issue-1', true),
      () => api.matchCode('issue-1', false),
      () => api.fixIt('issue-1'),
      () => api.searchKey('booking/123', 90),
      () => api.searchSummary('booking/123', 45),
      () => api.createAdhocIssue('orders', 'boom', 'telikos-dev'),
      () => api.getNamespaceSummary(true),
      () => api.getDashboard(12),
      () => api.getVibeUsage(),
      () => api.sendChat('issue-1', [{ role: 'user', content: 'help' }]),
    ]

    for (const fn of calls) {
      await fn()
    }

    expect(fetch).toHaveBeenNthCalledWith(1, '/api/status', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(2, '/api/issues', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(3, '/api/namespaces', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(4, '/api/namespace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer id-token' },
      body: JSON.stringify({ namespace: 'iom-preprod' }),
    })
    expect(fetch).toHaveBeenNthCalledWith(5, '/api/issues/issue-1/analyze?refresh=true', {
      method: 'POST',
      headers: { Authorization: 'Bearer id-token' },
    })
    expect(fetch).toHaveBeenNthCalledWith(6, '/api/issues/issue-1/code-match?refresh=false', {
      method: 'POST',
      headers: { Authorization: 'Bearer id-token' },
    })
    expect(fetch).toHaveBeenNthCalledWith(7, '/api/issues/issue-1/fix-it', {
      method: 'POST',
      headers: { Authorization: 'Bearer id-token' },
    })
    expect(fetch).toHaveBeenNthCalledWith(8, '/api/search?key=booking%2F123&minutes=90', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(9, '/api/search/summary?key=booking%2F123&minutes=45', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(10, '/api/issues/adhoc', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer id-token' },
      body: JSON.stringify({ service: 'orders', message: 'boom', namespace: 'telikos-dev' }),
    })
    expect(fetch).toHaveBeenNthCalledWith(11, '/api/summary?refresh=true', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(12, '/api/analytics?hours=12', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(13, '/api/analytics/vibe-usage', { headers: { Authorization: 'Bearer id-token' } })
    expect(fetch).toHaveBeenNthCalledWith(14, '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer id-token' },
      body: JSON.stringify({ issue_id: 'issue-1', messages: [{ role: 'user', content: 'help' }] }),
    })
  })

  it('returns empty auth headers when there is no active account', async () => {
    getActiveAccount.mockReturnValue(null)
    getAllAccounts.mockReturnValue([])
    const api = await import('./api.js')
    await api.listIssues()
    expect(fetch).toHaveBeenCalledWith('/api/issues', { headers: {} })
  })

  it('redirects to login when silent token acquisition fails', async () => {
    acquireTokenSilent.mockRejectedValue(new Error('expired'))
    const api = await import('./api.js')
    await api.listNamespaces()
    expect(loginRedirect).toHaveBeenCalledWith({ scopes: ['openid'] })
    expect(fetch).toHaveBeenCalledWith('/api/namespaces', { headers: {} })
  })

  it('throws on unauthorized responses and triggers login redirect', async () => {
    fetch.mockResolvedValueOnce(makeResponse({ ok: false, status: 401, statusText: 'Unauthorized' }))
    const api = await import('./api.js')
    await expect(api.getStatus()).rejects.toThrow('Session expired — please sign in again')
    expect(loginRedirect).toHaveBeenCalled()
  })

  it('throws for non-401 HTTP failures', async () => {
    fetch.mockResolvedValueOnce(makeResponse({ ok: false, status: 500, statusText: 'Server Error' }))
    const api = await import('./api.js')
    await expect(api.getStatus()).rejects.toThrow('500 Server Error')
  })
})
