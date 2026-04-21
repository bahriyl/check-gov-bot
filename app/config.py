from dataclasses import dataclass
import os


@dataclass
class Settings:
    bot_token: str
    playwright_headless: bool = True
    http_timeout_seconds: int = 20
    provider_refresh_hours: int = 6
    ocr_provider: str = "paddle"
    docai_timeout_seconds: int = 30
    google_application_credentials: str | None = None
    docai_project_id: str | None = None
    docai_location: str | None = None
    docai_processor_id: str | None = None



def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}



def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    ocr_provider = os.getenv("OCR_PROVIDER", "paddle").strip().lower() or "paddle"
    if ocr_provider not in {"paddle", "docai"}:
        raise RuntimeError("OCR_PROVIDER must be one of: paddle, docai")

    settings = Settings(
        bot_token=token,
        playwright_headless=_env_bool("PLAYWRIGHT_HEADLESS", True),
        http_timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        provider_refresh_hours=int(os.getenv("PROVIDER_REFRESH_HOURS", "6")),
        ocr_provider=ocr_provider,
        docai_timeout_seconds=int(os.getenv("DOCAI_TIMEOUT_SECONDS", "30")),
        google_application_credentials=os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        docai_project_id=os.getenv("DOCAI_PROJECT_ID"),
        docai_location=os.getenv("DOCAI_LOCATION"),
        docai_processor_id=os.getenv("DOCAI_PROCESSOR_ID"),
    )

    if settings.ocr_provider == "docai":
        required = {
            "GOOGLE_APPLICATION_CREDENTIALS": settings.google_application_credentials,
            "DOCAI_PROJECT_ID": settings.docai_project_id,
            "DOCAI_LOCATION": settings.docai_location,
            "DOCAI_PROCESSOR_ID": settings.docai_processor_id,
        }
        missing = [name for name, value in required.items() if not (value and value.strip())]
        if missing:
            raise RuntimeError(
                "OCR_PROVIDER=docai requires env vars: " + ", ".join(sorted(missing))
            )

    return settings
