from functools import lru_cache
from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv()



def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [item.strip().upper() for item in raw.split(",") if item.strip()]

def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


class Settings(BaseModel):
    qbo_client_id: str = os.getenv("QBO_CLIENT_ID", "")
    qbo_client_secret: str = os.getenv("QBO_CLIENT_SECRET", "")
    qbo_redirect_uri: str = os.getenv("QBO_REDIRECT_URI", "http://localhost:8000/qbo/callback")
    qbo_env: str = os.getenv("QBO_ENV", "sandbox")
    qbo_minor_version: str = os.getenv("QBO_MINOR_VERSION", "")
    app_secret_key: str = os.getenv("APP_SECRET_KEY", "change_me")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./quote_margin.db")
    qbo_cf_margin_id: str = os.getenv("QBO_CF_MARGIN_ID", "1")
    qbo_cf_profit_id: str = os.getenv("QBO_CF_PROFIT_ID", "2")
    qbo_cf_profit_per_hour_id: str = os.getenv("QBO_CF_PROFIT_PER_HOUR_ID", "3")
    qbo_cf_sph_id: str = os.getenv("QBO_CF_SPH_ID", os.getenv("QBO_CF_PROFIT_PER_HOUR_ID", "3"))
    qbo_cf_sph_name: str = os.getenv("QBO_CF_SPH_NAME", "SPH")
    variable_cost_item_codes: list[str] = _env_list("VARIABLE_COST_ITEM_CODES", "MC,MI,MP")
    labor_item_prefixes: list[str] = _env_list("LABOR_ITEM_PREFIXES", "LC:")
    default_estimate_refresh_days: int = int(os.getenv("DEFAULT_ESTIMATE_REFRESH_DAYS", "30"))
    app_username: str = os.getenv("APP_USERNAME", "")
    app_password: str = os.getenv("APP_PASSWORD", "")
    require_basic_auth: bool = _env_bool("REQUIRE_BASIC_AUTH", "false")
    qbo_read_only: bool = _env_bool("QBO_READ_ONLY", "true")
    secure_cookies: bool = _env_bool("SECURE_COOKIES", "false")
    enable_hsts: bool = _env_bool("ENABLE_HSTS", "false")

    @property
    def qbo_api_base_url(self) -> str:
        if self.qbo_env.lower() == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"

    @property
    def normalized_qbo_env(self) -> str:
        return "production" if self.qbo_env.lower() == "production" else "sandbox"


@lru_cache
def get_settings() -> Settings:
    return Settings()
