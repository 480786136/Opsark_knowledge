from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./data/knowledge.db"
    knowledge_service_token: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 1536
    embedding_index_version: str = "default-v1"

    @property
    def embedding_enabled(self):
        return bool(
            self.embedding_base_url and self.embedding_api_key and self.embedding_model
        )


@lru_cache
def settings():
    return Settings()
