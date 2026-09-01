from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Mode
    use_mock: bool = True

    # Log source: "k8s" (read pod logs via kubeconfig), "loki" (direct), or
    # "grafana" (Loki via Grafana datasource proxy)
    log_source: str = "grafana"

    k8s_namespace: str = "iom-preprod"

    # K8s direct (used when log_source=k8s)
    kubeconfig: str = ""          # path to kubeconfig; blank = in-cluster or ~/.kube/config
    k8s_context: str = ""         # optional named context
    k8s_tail_lines: int = 200     # log lines to pull per container per poll
    k8s_error_pattern: str = r"(?i)\b(error|exception|fatal|panic|traceback)\b"

    # Loki (direct)
    loki_url: str = "http://localhost:3100"

    # Grafana datasource proxy (no cluster access needed)
    grafana_url: str = "https://grafana.maersk.io"
    grafana_token: str = ""
    grafana_datasource_uid: str = "loki"
    loki_query: str = '{namespace="iom-preprod", k8s_cluster=~"ohp-np-west-1|ohp-np-north-1|ohp-prod-west-1|ohp-prod-north-1|clm-prod-westeurope-1", app!~"tfr-backend|tfr-frontend"} |~ "(?i)\\b(error|exception|fatal|panic|traceback)\\b"'
    lookback_seconds: int = 300
    poll_interval_seconds: int = 15
    max_lines_per_poll: int = 1000
    loki_timeout_seconds: int = 60  # increase for large/busy namespaces

    # LLM provider: "openai" or "anthropic"
    llm_provider: str = "openai"

    # OpenAI (or Azure OpenAI — auto-detected when base_url is an azure.com host)
    openai_api_key: str = ""
    openai_base_url: str = "https://vibe-proxy.westeurope.dev.maersk.io/"
    openai_api_version: str = "2024-10-21"  # Azure only
    openai_analyze_model: str = "claude-sonnet-4-6"
    openai_chat_model: str = "claude-sonnet-4-6"

    @property
    def is_azure_openai(self) -> bool:
        return "azure.com" in self.openai_base_url.lower()

    # Anthropic
    anthropic_api_key: str = ""
    analyze_model: str = "claude-opus-4-8"
    chat_model: str = "claude-sonnet-4-6"

    # GitHub (code matching)
    github_token: str = ""               # blank = fall back to `gh auth token`
    github_org: str = "Maersk-Global"
    github_api: str = "https://api.github.com"
    code_match_max_files: int = 4
    code_match_max_file_lines: int = 220

    # Clusters to include in Loki queries (comma-separated).
    # BLANK = every cluster (the selector becomes k8s_cluster=~".+"), which still
    # satisfies Maersk Loki's 2-label minimum. Name clusters explicitly only to
    # narrow the blast radius.
    k8s_clusters: str = ""

    @property
    def k8s_cluster_list(self) -> list:
        return [c.strip() for c in self.k8s_clusters.split(",") if c.strip()]

    # Namespace prefixes the key search fans out over (comma-separated), applied
    # to the namespace list Loki reports.
    # BLANK = every non-system namespace. Set e.g. "iom-,telikos-" to narrow the
    # fan-out if searches get slow: cost is roughly (namespaces x time chunks)
    # Loki queries per search.
    search_namespace_prefixes: str = ""

    @property
    def search_namespace_prefix_list(self) -> list:
        return [p.strip() for p in self.search_namespace_prefixes.split(",") if p.strip()]

    # ServiceNow integration
    snow_instance_url: str = "https://maersk.service-now.com"
    snow_client_id: str = ""    # Azure AD app client_id with SNOW permission
    snow_client_secret: str = ""  # Azure AD app client_secret
    # Legacy basic auth (unused if snow_client_id+secret are set)
    snow_username: str = ""
    snow_password: str = ""

    # Knowledge base (RAG) — pgvector when DATABASE_URL is set, SQLite fallback otherwise
    embedding_model: str = "text-embedding-3-small"
    kb_confidence_threshold: float = 0.85  # cosine similarity; below this, L1 defers to L2/LLM

    # Approved web search (Tavily) — used by the incident-analysis fallback when the
    # knowledge base has no confident match. Domains outside the allowlist are never searched.
    tavily_api_key: str = ""
    web_search_allowed_domains: str = ""  # comma-separated, e.g. "kubernetes.io,spring.io"

    @property
    def web_search_allowed_domain_list(self) -> List[str]:
        return [d.strip() for d in self.web_search_allowed_domains.split(",") if d.strip()]

    # Server
    cors_origins: str = "https://first-responder.sit.maersk-digital.net"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Azure AD SSO (user login)
    sso_enabled: bool = True
    azure_ad_tenant_id: str = "05d75c05-fa1a-42e7-9cf1-eb416c396f2d"
    azure_ad_client_id: str = "fdb5c5f4-d589-495b-8b84-c0f3abad2eb0"

    # Analytics database — PostgreSQL in prod, SQLite fallback for local dev.
    # Set to a PostgreSQL URL in production:
    #   postgresql://user:pass@host:5432/dbname?ssl=true
    # Leave blank to use SQLite at backend/data/usage.db.
    database_url: str = ""

    @property
    def llm_enabled(self) -> bool:
        if self.use_mock:
            return False
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return False


settings = Settings()
