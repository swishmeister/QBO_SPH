from functools import lru_cache
from pydantic import BaseModel
from dotenv import load_dotenv
import os

load_dotenv()


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
    app_username: str = os.getenv("APP_USERNAME", "")
    app_password: str = os.getenv("APP_PASSWORD", "")
    require_basic_auth: bool = os.getenv("REQUIRE_BASIC_AUTH", "false").lower() in {"1", "true", "yes", "on"}
    qbo_read_only: bool = os.getenv("QBO_READ_ONLY", "false").lower() in {"1", "true", "yes", "on"}

    @property
    def qbo_api_base_url(self) -> str:
        if self.qbo_env.lower() == "production":
            return "https://quickbooks.api.intuit.com"
        return "https://sandbox-quickbooks.api.intuit.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
