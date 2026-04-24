from dataclasses import dataclass, field
import os


@dataclass
class Settings:
    bot_token: str
    bot_handler_workers: int = 8
    playwright_headless: bool = True
    http_timeout_seconds: int = 20
    provider_refresh_hours: int = 6
    checkgov_global_parallel_limit: int = 8
    checkgov_per_user_parallel_limit: int = 2
    binance_base_url: str = "https://api.binance.com"
    binance_timeout_seconds: int = 20
    binance_api_key: str | None = None
    binance_secret_key: str | None = None
    binance_test_non_active_order_numbers: list[str] = field(default_factory=list)
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


def _env_csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    items = [item.strip() for item in raw.split(",")]
    # Keep input order while dropping empty values and duplicates.
    return list(dict.fromkeys([item for item in items if item]))



def load_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set")

    ocr_provider = os.getenv("OCR_PROVIDER", "paddle").strip().lower() or "paddle"
    if ocr_provider not in {"paddle", "docai"}:
        raise RuntimeError("OCR_PROVIDER must be one of: paddle, docai")

    settings = Settings(
        bot_token=token,
        bot_handler_workers=int(os.getenv("BOT_HANDLER_WORKERS", "8")),
        playwright_headless=_env_bool("PLAYWRIGHT_HEADLESS", True),
        http_timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
        provider_refresh_hours=int(os.getenv("PROVIDER_REFRESH_HOURS", "6")),
        checkgov_global_parallel_limit=int(os.getenv("CHECKGOV_GLOBAL_PARALLEL_LIMIT", "8")),
        checkgov_per_user_parallel_limit=int(os.getenv("CHECKGOV_PER_USER_PARALLEL_LIMIT", "2")),
        binance_base_url=os.getenv("BINANCE_BASE_URL", "https://api.binance.com").strip()
        or "https://api.binance.com",
        binance_timeout_seconds=int(os.getenv("BINANCE_TIMEOUT_SECONDS", "20")),
        binance_api_key=os.getenv("BINANCE_API_KEY"),
        binance_secret_key=os.getenv("BINANCE_SECRET_KEY"),
        binance_test_non_active_order_numbers=_env_csv("BINANCE_TEST_NON_ACTIVE_ORDER_NUMBERS"),
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

    if settings.bot_handler_workers < 1:
        raise RuntimeError("BOT_HANDLER_WORKERS must be >= 1")
    if settings.checkgov_global_parallel_limit < 1:
        raise RuntimeError("CHECKGOV_GLOBAL_PARALLEL_LIMIT must be >= 1")
    if settings.checkgov_per_user_parallel_limit < 1:
        raise RuntimeError("CHECKGOV_PER_USER_PARALLEL_LIMIT must be >= 1")

    return settings
