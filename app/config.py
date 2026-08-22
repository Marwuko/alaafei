from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./alaafei_dev.db"
    whatsapp_verify_token: str = "dev-verify-token"
    whatsapp_app_secret: str = "dev-app-secret"
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    escalation_hours: int = 48

    class Config:
        env_file = ".env"


settings = Settings()
