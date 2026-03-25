"""Trading configuration loaded from environment variables."""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # MEXC API
    API_KEY: str = os.getenv("MEXC_API_KEY", "")
    SECRET_KEY: str = os.getenv("MEXC_SECRET_KEY", "")
    BASE_URL: str = "https://contract.mexc.com"
    SPOT_BASE_URL: str = "https://api.mexc.com"

    # Trading parameters
    LEVERAGE: int = int(os.getenv("LEVERAGE", "10"))
    TRADE_AMOUNT_USDT: float = float(os.getenv("TRADE_AMOUNT_USDT", "100"))
    MAX_POSITION_SIZE: float = float(os.getenv("MAX_POSITION_SIZE", "1000"))

    # Risk management
    STOP_LOSS_PERCENT: float = float(os.getenv("STOP_LOSS_PERCENT", "2.0"))
    TAKE_PROFIT_PERCENT: float = float(os.getenv("TAKE_PROFIT_PERCENT", "3.0"))

    # Strategy
    PRICE_DEVIATION_THRESHOLD: float = float(
        os.getenv("PRICE_DEVIATION_THRESHOLD", "0.002")
    )
    CHECK_INTERVAL_SECONDS: int = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))

    # Trading pair
    SYMBOL: str = "USDC_USDT"

    @classmethod
    def validate(cls) -> bool:
        if not cls.API_KEY or not cls.SECRET_KEY:
            raise ValueError(
                "MEXC_API_KEY and MEXC_SECRET_KEY must be set in .env file"
            )
        return True
