from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    BOT_TOKEN: str
    WEBHOOK_HOST: str = ""
    WEBHOOK_PATH: str = "/webhook"
    WEBHOOK_SECRET: str = "change-me"
    LISTEN_HOST: str = "0.0.0.0"
    LISTEN_PORT: int = 8080
    DATABASE_URL: str = "sqlite+aiosqlite:///./remind.db"
    LOG_LEVEL: str = "INFO"
    RATE_LIMIT: int = 20  # messages per minute per user


settings = Settings()  # type: ignore[call-arg]
