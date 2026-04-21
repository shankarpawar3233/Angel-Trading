from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class OSISettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OSI_", extra="ignore")

    app_name: str = "Options Signal Intelligence"
    app_host: str = "0.0.0.0"
    app_port: int = 8010

    redis_url: str = "redis://localhost:6379/0"
    postgres_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/osi"

    min_signal_confidence: float = 40.0
    target_multiplier: float = 1.4
    sl_multiplier: float = 0.8
    partial_target_multiplier: float = 1.2
    market_exit_time_ist: str = "15:25"
    daily_loss_cap: float = 3000.0
    max_consecutive_sl: int = 3

    use_sample_data: bool = True
    sample_tick_interval_ms: int = 200

    websocket_client_code: str = ""
    angel_api_key: str = ""
    angel_client_id: str = ""
    angel_password: str = ""
    angel_totp_secret: str = ""
    angel_feed_token: str = ""
    holidays_csv: str = ""
    ws_reconnect_max_backoff_sec: int = 60
    ws_heartbeat_stale_sec: int = 20


settings = OSISettings()

