"""Application settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    store_id: str
    banner: str
    pcx_api_key: str
    pcx_api_base: str
    host: str
    port: int
    log_level: str
    mcp_token: str
    public_url: str
    deal_scan_hours: float = 24.0  # refresh the deal catalogue this often; 0 disables the background scan
    allowed_hosts: list[str] = field(default_factory=list)
    # -- secrets, people ---------------------------------------------------------------------
    master_key_b64: str = ""  # MASTER_KEY: 32 random bytes, base64; encrypts the secrets vault and PC ID tokens
    pcid_client_secret: str = ""  # PCID_CLIENT_SECRET: the PC Express Android app's OAuth client secret
    # -- who may use the app: Cloudflare Access ---------------------------------------------
    access_team: str = ""  # ACCESS_TEAM_DOMAIN, e.g. "yourteam.cloudflareaccess.com"
    access_aud: str = ""  # ACCESS_AUD: the Access application's Audience (AUD) tag
    # ALLOWED_USERS: "email=Name,email=Name". Only these emails get in, even if Access lets others through.
    allowed_users: dict[str, str] = field(default_factory=dict)
    region: str = "Canada"  # HOUSEHOLD_REGION: where you live, for the assistant (units, seasons, shops)
    dev_user: str = ""  # DEV_USER: local development only; skips Access (refused unless HOST is loopback)
    client_ip_header: str = ""  # header with the real client IP set by the proxy (Cloudflare: cf-connecting-ip)
    mcp_enabled: bool = False  # optional MCP endpoint at /mcp (for Claude Desktop/Code); bearer-token protected
    timezone: str = "America/Toronto"
    monthly_budget_usd: float = 10.0  # default LLM spend cap; adjustable at runtime from the app
    jobs_enabled: bool = True
    trusted_proxies: str = ""  # FORWARDED_ALLOW_IPS: proxy addresses whose X-Forwarded-For is trusted

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def household(self) -> str:
        """For prompts: "Alex and Sam (Ontario, Canada)", from ALLOWED_USERS and HOUSEHOLD_REGION."""
        names = list(dict.fromkeys(self.allowed_users.values()))
        who = " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names) or "a household"
        return f"{who} ({self.region})"

    @property
    def pcid_dir(self) -> Path:
        return self.data_dir / "pcid"

    @property
    def raw_dir(self) -> Path:
        """Recent raw retailer responses, kept for debugging (secrets are never in these)."""
        return self.data_dir / "raw"

    @property
    def master_key_file(self) -> Path:
        """Fallback key location for development only; production sets MASTER_KEY in the environment."""
        return self.data_dir / "master.key"


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def _users(spec: str) -> dict[str, str]:
    """"a@x.com=Alex, b@y.com=Sam" -> {"a@x.com": "Alex", ...} (emails lower-cased)."""
    out = {}
    for part in spec.split(","):
        email, _, name = part.strip().partition("=")
        if email.strip():
            out[email.strip().lower()] = name.strip() or email.split("@")[0]
    return out


def load_settings() -> Settings:
    _load_dotenv(Path(os.environ.get("SHOPPING_ENV_FILE", ".env")))
    data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
    port = int(os.environ.get("PORT", "8000"))
    public_url = os.environ.get("PUBLIC_URL", f"http://localhost:{port}").rstrip("/")
    # Hosts the app answers to (DNS-rebinding protection): loopback, PUBLIC_URL's host, plus ALLOWED_HOSTS.
    hosts = ",".join(["localhost", "127.0.0.1", urlsplit(public_url).hostname or "", os.environ.get("ALLOWED_HOSTS", "")])
    return Settings(
        data_dir=data_dir,
        store_id=os.environ.get("STORE_ID", ""),
        banner=os.environ.get("BANNER", "nofrills"),
        pcx_api_key=os.environ.get("PCX_API_KEY", ""),
        pcx_api_base=os.environ.get("PCX_API_BASE", "https://api.pcexpress.ca/pcx-bff/api"),
        host=os.environ.get("HOST", "127.0.0.1"),
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        mcp_token=os.environ.get("MCP_TOKEN", ""),
        public_url=public_url,
        deal_scan_hours=float(os.environ.get("DEAL_SCAN_HOURS", "24")),
        allowed_hosts=list(dict.fromkeys(h.strip() for h in hosts.split(",") if h.strip())),
        master_key_b64=os.environ.get("MASTER_KEY", ""),
        pcid_client_secret=os.environ.get("PCID_CLIENT_SECRET", "").strip(),
        access_team=os.environ.get("ACCESS_TEAM_DOMAIN", "").strip().removeprefix("https://").rstrip("/"),
        access_aud=os.environ.get("ACCESS_AUD", "").strip(),
        allowed_users=_users(os.environ.get("ALLOWED_USERS", "")),
        region=os.environ.get("HOUSEHOLD_REGION", "Canada").strip() or "Canada",
        dev_user=os.environ.get("DEV_USER", "").strip(),
        client_ip_header=os.environ.get("CLIENT_IP_HEADER", "").strip().lower(),
        mcp_enabled=_bool("MCP_ENABLED", False),
        timezone=os.environ.get("TZ", "America/Toronto"),
        monthly_budget_usd=float(os.environ.get("MONTHLY_BUDGET_USD", "10")),
        jobs_enabled=_bool("JOBS_ENABLED", True),
        trusted_proxies=os.environ.get("FORWARDED_ALLOW_IPS", ""),
    )


settings = load_settings()
