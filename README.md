# K8s Issue Assistant

A web app that proactively monitors a Kubernetes namespace, surfaces live errors
on a dashboard, and runs a Claude-powered chatbot that explains each error and
suggests fixes.

**Status: MVP.** Runs out-of-the-box on synthetic data (mock mode), then wires
into your real Loki + Claude with two env vars.

```
Loki (LogQL) ──poll──▶ FastAPI backend ──▶ group/dedupe ──▶ in-memory store
                              │                                    │
                         Claude API ◀───────── REST ──────── React dashboard
                       (analyze + chat)                     (live feed + chat)
```

## Architecture

| Layer | What it does | Where |
|-------|--------------|-------|
| Poller | Queries Loki every N seconds for error-level lines | `backend/app/poller.py` |
| Grouping | Fingerprints log lines (strips numbers/UUIDs/IPs) so duplicates collapse into one issue with a count | `backend/app/grouping.py` |
| Store | Keeps current issues + cached AI analyses in memory | `backend/app/store.py` |
| LLM | Claude calls for structured analysis (tool use) and free-form chat | `backend/app/llm.py` |
| API | `/api/issues`, `/api/issues/{id}/analyze`, `/api/chat`, `/api/status` | `backend/app/routes/` |
| UI | Live issue list (5s refresh), detail, AI analysis, chat | `frontend/src/App.jsx` |

## Quick start (mock mode — no infra needed)

**Backend**
```bash
cd backend
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env          # USE_MOCK=true by default
./.venv/bin/python -m uvicorn app.main:app --port 8077 --reload
```

**Frontend**
```bash
cd frontend
npm install
npm run dev                   # http://localhost:5173 (proxies /api to :8077)
```

You'll see synthetic errors streaming in, groupable, with mock AI analysis/chat.

## Going live

### 1. Connect an LLM
The LLM layer is provider-pluggable (`backend/app/llm.py`). Pick one in `backend/.env`:

**OpenAI** (default)
```
USE_MOCK=false
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_ANALYZE_MODEL=gpt-4o
OPENAI_CHAT_MODEL=gpt-4o-mini
```

**Azure OpenAI** — set an `azure.com` endpoint and the app auto-switches to the Azure client.
The model fields must be your **deployment names**, not model names.
```
USE_MOCK=false
LLM_PROVIDER=openai
OPENAI_API_KEY=<azure key>
OPENAI_BASE_URL=https://<resource>.openai.azure.com/
OPENAI_API_VERSION=2024-10-21
OPENAI_ANALYZE_MODEL=<deployment name>   # e.g. gpt-4.1-mini
OPENAI_CHAT_MODEL=<deployment name>
```

**Anthropic / Claude**
```
USE_MOCK=false
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
ANALYZE_MODEL=claude-opus-4-8
CHAT_MODEL=claude-sonnet-4-6
```

### 2. Connect logs

Three interchangeable sources, set via `LOG_SOURCE`. **Default is `k8s`.**

#### Option A — direct K8s (default, `LOG_SOURCE=k8s`)
Reads pod logs straight from the Kubernetes API with a kubeconfig — no Loki needed.
1. Apply the read-only ServiceAccount and mint a kubeconfig you control (see step 3 below),
   or use an existing kubeconfig.
2. In `backend/.env`:
   ```
   LOG_SOURCE=k8s
   K8S_NAMESPACE=telikos
   KUBECONFIG=/Users/you/.kube/telikos-config   # blank = in-cluster / ~/.kube/config
   K8S_CONTEXT=                                  # optional named context
   K8S_TAIL_LINES=200
   ```
3. Smoke-test access first:
   ```bash
   KUBECONFIG=~/.kube/telikos-config kubectl -n telikos get pods
   KUBECONFIG=~/.kube/telikos-config kubectl -n telikos logs <some-pod> --tail=5
   ```
Notes: error filtering happens client-side via `K8S_ERROR_PATTERN`. K8s logs are
ephemeral (lost on pod restart) — for long history prefer a Loki-backed source below.

#### Option B — via Grafana (`LOG_SOURCE=grafana`, no kubeconfig)
The app queries Loki *through* Grafana's datasource proxy.
1. In Grafana: **Administration → Service accounts → Add** (Viewer role) → **Add token**. Copy it.
2. Find the Loki datasource UID:
   ```bash
   curl -H "Authorization: Bearer $GRAFANA_TOKEN" "$GRAFANA_URL/api/datasources" | jq '.[] | {name, uid, type}'
   ```
3. In `backend/.env`:
   ```
   LOG_SOURCE=grafana
   GRAFANA_URL=https://grafana.example.com
   GRAFANA_TOKEN=glsa_...
   GRAFANA_DATASOURCE_UID=<loki uid>
   K8S_NAMESPACE=telikos
   ```
4. Tune `LOKI_QUERY` to your label scheme and smoke-test through the proxy:
   ```bash
   curl -G -H "Authorization: Bearer $GRAFANA_TOKEN" \
     "$GRAFANA_URL/api/datasources/proxy/uid/$GRAFANA_DATASOURCE_UID/loki/api/v1/query_range" \
     --data-urlencode 'query={namespace="telikos"} |~ "(?i)error"' | jq '.data.result | length'
   ```

#### Option C — direct Loki (`LOG_SOURCE=loki`)
If Loki is in-cluster, port-forward it (using a kubeconfig you control — **never commit it**):
```bash
KUBECONFIG=~/.kube/telikos-config kubectl -n monitoring port-forward svc/loki 3100:3100
```
Then set `LOG_SOURCE=loki`, `LOKI_URL`, `K8S_NAMESPACE`, and tune `LOKI_QUERY`.

### 3. (Options A & C) Read-only cluster access
Don't use an admin kubeconfig. Apply the scoped ServiceAccount and mint a token from it:
```bash
kubectl apply -f deploy/rbac.yaml
kubectl -n telikos create token issue-assistant --duration=24h   # k8s 1.24+
```
RBAC grants only `get/list/watch` on `pods`, `pods/log`, `events` in the `telikos` namespace.

## Security notes
- `.env`, `kubeconfig*`, and `.kube/` are git-ignored. Keep secrets out of the repo.
- The app needs **read-only** access. Nothing here mutates cluster state.
- Claude receives log lines for analysis — review your data-handling policy before
  sending production logs to an external API (Azure OpenAI / self-hosted are pluggable
  alternatives via `backend/app/llm.py`).

## Roadmap (post-MVP)
- Direct K8s Events ingestion (complements Loki; survives where logs are thin)
- Persistent store (Postgres/Redis) + historical trends and alerting
- Issue resolution tracking / mute / acknowledge
- Slack/Teams notifications on new high-severity issues
- In-cluster deployment manifests (Deployment + Service + Ingress using the SA above)
