"""Application settings, overridable via environment variables (SAKURA_*)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = APP_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SAKURA_", env_file=".env")

    # Local llama.cpp server (OpenAI-compatible), as configured in opencode.
    llm_base_url: str = "http://localhost:8020/v1"
    llm_model: str = "qwen3.6-27b"
    llm_api_key: str = "none"  # llama.cpp ignores it but the header must exist
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4096
    # Local 27B inference is slow; allow long generations.
    llm_timeout_seconds: float = 600.0

    extract_max_iterations: int = 8
    rail_max_iterations: int = 40
    city_max_iterations: int = 18
    geocode_max_per_city: int = 6
    document_max_chars: int = 24_000

    # Self-hosted SearXNG (primary search provider). Empty string -> use the
    # DuckDuckGo scraping fallback only. See scripts/searxng.sh.
    searxng_url: str = "http://localhost:8888"
    # Start the local SearXNG container when the app launches (if it's not
    # already up). No boot/systemd involvement — only when you run the app.
    searxng_autostart: bool = True

    # Live public-transit fares via the google-maps-scraper `gmaps-fares`
    # container (real Google Maps transit fares incl. buses). Requires the
    # image to be built locally; set enabled=False to skip the tool entirely.
    gmaps_fares_enabled: bool = True
    gmaps_fares_image: str = "gmaps-scraper:fares"
    gmaps_fares_lang: str = "en"
    gmaps_fares_timeout_seconds: float = 150.0

    fares_path: Path = APP_DIR / "data" / "fares.json"
    places_path: Path = APP_DIR / "data" / "places.json"
    food_path: Path = APP_DIR / "data" / "food.json"
    frontend_dir: Path = REPO_DIR / "frontend"

    max_upload_bytes: int = 15 * 1024 * 1024


settings = Settings()
