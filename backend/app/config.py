from __future__ import annotations

from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Mode
    use_mock: bool = True

    # Log source: "k8s" (read pod logs via kubeconfig), "loki" (direct), or
    # "grafana" (Loki via Grafana datasource proxy)
    log_source: str = "k8s"

    k8s_namespace: str = "telikos"

    # K8s direct (used when log_source=k8s)
    kubeconfig: str = ""          # path to kubeconfig; blank = in-cluster or ~/.kube/config
    k8s_context: str = ""         # optional named context
    k8s_tail_lines: int = 200     # log lines to pull per container per poll
    k8s_error_pattern: str = r"(?i)\b(error|exception|fatal|panic|traceback)\b"

    # Loki (direct)
    loki_url: str = "http://localhost:3100"

    # Grafana datasource proxy (no cluster access needed)
    grafana_url: str = ""
    grafana_token: str = ""
    grafana_datasource_uid: str = ""
    loki_query: str = '{namespace="telikos"} |~ "(?i)\\b(error|exception|fatal|panic|traceback)\\b"'
    lookback_seconds: int = 300
    poll_interval_seconds: int = 15
    max_lines_per_poll: int = 1000
    loki_timeout_seconds: int = 60  # increase for large/busy namespaces

    # LLM provider: "openai" or "anthropic"
    llm_provider: str = "openai"

    # OpenAI (or Azure OpenAI — auto-detected when base_url is an azure.com host)
    openai_api_key: str = ""
    openai_base_url: str = ""  # Azure endpoint or proxy; blank = api.openai.com
    openai_api_version: str = "2024-10-21"  # Azure only
    openai_analyze_model: str = "gpt-4o"   # Azure: this is the DEPLOYMENT name
    openai_chat_model: str = "gpt-4o-mini"  # Azure: this is the DEPLOYMENT name

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

    # Clusters to include in the namespace list (comma-separated).
    # Namespaces are fetched from Loki filtered by these clusters.
    k8s_clusters: str = "ohp-np-west-1,ohp-prod-west-1"

    @property
    def k8s_cluster_list(self) -> list:
        return [c.strip() for c in self.k8s_clusters.split(",") if c.strip()]

    # Server
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    # Azure AD SSO (user login)
    sso_enabled: bool = False
    azure_ad_tenant_id: str = ""
    azure_ad_client_id: str = ""

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
