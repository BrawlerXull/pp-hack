import os
from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"
# The test suite sets CHECKOUT_GUARD_NO_ENV_FILE=1: the repo .env holds real Gemini/PayPal credentials and a
# test that forgot a mock must fail, not quietly call the real APIs.
ENV_FILES = None if os.environ.get("CHECKOUT_GUARD_NO_ENV_FILE") else (".env", "../.env")

# Values from the code and .env.example. A deployed demo with any of them is open to anyone who read the repo.
DEFAULT_ADMIN_PASSWORDS = frozenset({"", "guard-demo"})
DEFAULT_SESSION_SECRETS = frozenset({"", "dev-session-secret-change-me", "change-me-to-a-long-random-string"})
DEFAULT_SHOP_API_KEYS = frozenset({"", "dev-shop-key", "change-me-shop-key"})
MIN_SESSION_SECRET_LEN = 32


class Settings(BaseSettings):
    # hide_input_in_errors: a failed check (e.g. a production deploy missing a secret) would otherwise print a
    # truncated repr of every setting, real API keys included, into the deploy log.
    model_config = SettingsConfigDict(env_file=ENV_FILES, extra="ignore", hide_input_in_errors=True)

    # "production" makes startup refuse the well-known demo secrets (_no_default_secrets).
    environment: Literal["development", "production"] = "development"

    # PayPal sandbox REST app
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_api_base: str = SANDBOX_API_BASE
    paypal_webhook_id: str = ""

    # AI (Gemini)
    gemini_api_key: str = ""
    # Optional second key (another project's quota): tried only after every model failed on the first key.
    gemini_api_key_2: str = ""
    gemini_model: str = "gemini-3.5-flash"
    # Tried in order when the primary is overloaded (503), over quota or times out.
    gemini_fallback_models: str = "gemini-2.5-flash,gemini-flash-lite-latest"
    ai_timeout_s: float = 20.0
    # False: no Gemini call at all; the analyst returns the rules fallback labelled "AI disabled" and reports
    # use their templates. For a demo without a key or quota, and to show the rules-only path on purpose.
    ai_enabled: bool = True

    database_url: str = "postgresql+asyncpg://localhost:5432/checkout_guard"

    # Demo shop identity + auth
    shop_id: str = "wick-and-wax"
    shop_api_key: str = "dev-shop-key"  # header X-Guard-Key for the external /v1/guard/check API
    admin_password: str = "guard-demo"  # dashboard login (judge credentials go in README)
    session_secret: str = "dev-session-secret-change-me"

    # Public URL of the shop frontend: 3-D Secure return/cancel pages for SCA_ALWAYS card orders.
    public_app_url: str = "http://localhost:3000"

    # Simulator endpoints (/sim/*) are only mounted when this is true.
    simulator_enabled: bool = True

    # Worker cadence
    detect_interval_s: float = 5.0
    reassess_interval_s: float = 120.0

    # Proxies in front of the API that each append the address they saw to X-Forwarded-For. The client IP is
    # the entry that many places from the right (the left-most one is whatever the client typed). 0 (the
    # default): ignore the header and use the connection's address. Local dev must stay 0: the Next.js
    # rewrite forwards the browser's header unchanged, so trusting it would trust what the client typed.
    # On Render, 1: its edge appends the client address and Next passes it on to the private API. Check a
    # deploy with GET /dashboard/network-check.
    trusted_proxy_hops: int = 0

    # Human check before a challenged checkout (Cloudflare Turnstile). The defaults are Cloudflare's public
    # test keys: the widget always passes and says "for testing only". Empty secret: a plain click-through.
    turnstile_site_key: str = "1x00000000000000000000AA"
    turnstile_secret_key: str = "1x0000000000000000000000000000000AA"
    # Shown to a shopper the guard blocked, so a false positive has somewhere to go.
    shop_contact_email: str = "help@wickandwax.example"

    # The guard check gets this long before the checkout goes on without it (fail-open, app/guard/check.py).
    guard_check_budget_ms: int = 800

    # Shared hosted demo (only with SIMULATOR_ENABLED, app/simulator/janitor.py): re-seed the "normal"
    # history when its newest day is this old, and stand the demo down to defaults after this many idle
    # minutes (no checkout attempt, no owner action). 0 switches either off.
    demo_reseed_after_h: int = 36
    demo_idle_restore_min: int = 30

    # Optional: also push each owner alert to a Slack incoming webhook or an ntfy topic URL (app/notify.py).
    notify_webhook_url: str = ""
    notify_timeout_s: float = 3.0

    spike_output_dir: str = "spike-output"

    @field_validator("database_url")
    @classmethod
    def _asyncpg_driver(cls, v: str) -> str:
        # Render and most tools hand out postgres:// URLs; SQLAlchemy async needs the asyncpg driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix) :]
        return v

    @model_validator(mode="after")
    def _sandbox_only(self) -> "Settings":
        # Hackathon safety: this project must never talk to live PayPal.
        if "sandbox" not in self.paypal_api_base:
            raise ValueError(f"PAYPAL_API_BASE must be a sandbox URL, got {self.paypal_api_base!r}")
        return self

    @model_validator(mode="after")
    def _no_default_secrets(self) -> "Settings":
        # The session secret signs owner sessions and challenge tokens: a known one lets anyone forge a login.
        if self.environment != "production":
            return self
        problems = []
        if self.admin_password in DEFAULT_ADMIN_PASSWORDS:
            problems.append("ADMIN_PASSWORD is the demo default")
        if (
            self.session_secret in DEFAULT_SESSION_SECRETS
            or len(self.session_secret) < MIN_SESSION_SECRET_LEN
        ):
            problems.append(f"SESSION_SECRET must be a random string of {MIN_SESSION_SECRET_LEN}+ characters")
        if self.shop_api_key in DEFAULT_SHOP_API_KEYS:
            problems.append("SHOP_API_KEY is the demo default")
        if problems:
            raise ValueError("ENVIRONMENT=production refuses default secrets: " + "; ".join(problems))
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
