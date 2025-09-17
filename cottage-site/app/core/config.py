from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional


class Settings(BaseSettings):
    APP_NAME: str = "Cottage-Dev.net"
    ENV: str = "development"
    DEBUG: bool = True

    SECRET_KEY: str = "dev-secret"  # default for development
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_ALGORITHM: str = "HS256"
    JWT_COOKIE_NAME: str = "access_token"
    JWT_COOKIE_SECURE: bool = False  # set True in production with HTTPS

    # Default to local Postgres from docker-compose
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/cottage_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    ALLOWED_HOSTS: List[str] = ["*"]

    MEILI_URL: Optional[str] = None
    MEILI_MASTER_KEY: Optional[str] = None

    # OAuth credentials (optional). Set in .env if using Google/GitHub login
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URL: Optional[str] = None  # e.g. http://localhost:8000/auth/google/callback
    GITHUB_CLIENT_ID: Optional[str] = None
    GITHUB_CLIENT_SECRET: Optional[str] = None
    GITHUB_REDIRECT_URL: Optional[str] = None  # e.g. http://localhost:8000/auth/github/callback

    # Cloudflare Turnstile (optional but recommended)
    TURNSTILE_SITE_KEY: Optional[str] = None
    TURNSTILE_SECRET_KEY: Optional[str] = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
