from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = Path("data")
    generation_timeout: float = 120.0
    login_timeout: float = 180.0
    max_concurrent: int = 1
    headless: bool = False


def get_settings() -> Settings:
    return Settings()
