from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# pydantic-settings reads .env only for declared fields; load_dotenv exposes all keys to os.getenv
# (e.g. TELEGRAM_BOT_TOKEN) used by optional integrations.
load_dotenv(PROJECT_ROOT / ".env", override=False)

DATA_DIR = PROJECT_ROOT / "storage"
DB_PATH = DATA_DIR / "market_data.sqlite"
LOG_DIR = DATA_DIR / "logs"


class Settings(BaseSettings):
    environment: Literal["dev", "prod"] = Field(default="dev")
    port: int = Field(default=8000, description="API server port")

    # Market configuration
    indices: tuple[str, ...] = ("NIFTY", "SENSEX")
    yahoo_symbols: dict[str, str] = {
        "NIFTY": "^NSEI",
        "SENSEX": "^BSESN",
    }

    # Database
    sqlite_url: str = Field(default=f"sqlite:///{DB_PATH}")

    # NSE endpoints (unofficial, public)
    nse_option_chain_url: str = (
        "https://www.nseindia.com/api/option-chain-indices?symbol={symbol}"
    )

    # Historical data config
    historical_days: int = 365
    intraday_intervals: tuple[str, ...] = ("1m", "5m", "15m")
    daily_interval: str = "1d"

    # Model config
    sequence_length: int = 60  # last 60 candles

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
