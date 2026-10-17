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

    # --- 保活（持久化 profile + 定时访问豆包页面，让浏览器自己续期）---
    keepalive_enabled: bool = False
    # 相对路径按 data_dir 解析，避免 serve 的工作目录一变就换了个空 profile
    profile_dir: Path = Path("browser-profile")
    keepalive_interval: float = 6 * 3600.0
    # 每次访问页面后等多久（毫秒），留给页面发请求/服务端续签
    keepalive_page_wait_ms: float = 10000.0
    # 有头模式用系统 Chrome 更像真人；留空则用 Playwright 自带 Chromium
    browser_channel: str | None = None

    @property
    def resolved_profile_dir(self) -> Path:
        return (
            self.profile_dir
            if self.profile_dir.is_absolute()
            else self.data_dir / self.profile_dir
        )


def get_settings() -> Settings:
    return Settings()
