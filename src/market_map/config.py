"""Настройки приложения из переменных окружения (и файла .env)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "market_map"
    postgres_user: str = "market_map"
    postgres_password: str = "market_map"

    # 1С: стандартный интерфейс OData. Если адрес не задан, загружаются файлы выгрузки.
    onec_odata_url: str | None = None
    onec_user: str = ""
    onec_password: str = ""
    onec_files_dir: str = "data/samples/onec"

    # Подготовленная база конкурентов: адрес SQLAlchemy
    prepared_db_url: str = "sqlite:///data/samples/prepared/prepared.sqlite"

    # Планировщик: ежедневный запуск конвейера обновления
    schedule_time: str = "03:00"
    schedule_tz: str = "Europe/Moscow"
    schedule_run_on_start: bool = True

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
