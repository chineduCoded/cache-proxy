from functools import lru_cache
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    origin: str = ""
    port: int = 5000

@lru_cache
def get_settings() -> Settings:
    return Settings()