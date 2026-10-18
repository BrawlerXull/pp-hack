from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_API_BASE = "https://api-m.sandbox.paypal.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    # PayPal sandbox REST app
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_api_base: str = SANDBOX_API_BASE
    paypal_webhook_id: str = ""

    # AI (Gemini)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    # Tried in order when the primary is overloaded (503), over quota or times out.
    gemini_fallback_models: str = "gemini-2.5-flash,gemini-flash-lite-latest"
    ai_timeout_s: float = 20.0

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
