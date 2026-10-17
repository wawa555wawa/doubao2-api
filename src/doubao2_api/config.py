from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 9756
    data_dir: Path = Path("data")
    generation_timeout: float = 120.0
    login_timeout: float = 180.0
    max_concurrent: int = 1
    headless: bool = False
    # 登录态剩余不足这么多天时，启动日志提前告警（豆包 sid_guard 是 30 天硬过期）
    session_warn_days: float = 3.0


def get_settings() -> Settings:
    return Settings()
