"""Load API keys and other secrets from a local .env file.

This lets capture/analysis scripts find their credentials even when the
controlling process (scheduled job, LAN receiver, batch file) was started in a
shell that did not export the keys.
"""
import os
from pathlib import Path


ROOT_ENV = Path(__file__).resolve().parent / "config" / "doubao_api_keys.env"
ALT_ENV = Path(__file__).resolve().parent / "doubao_api_keys.env"
DEEPSEEK_SECRET = Path(__file__).resolve().parent / "config" / "deepseek_api_key.secret"


_ENV_KEYS = (
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DOUBAO_PRODUCT_AI_MODE",
    "DOUBAO_PRODUCT_AI_MODEL",
    "DOUBAO_AI_TIMEOUT",
    "DOUBAO_META_TIMEOUT",
    "DOUBAO_USE_AI_PRODUCT",
    "DOUBAO_USE_AI_SOURCE",
)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            # Only set if the environment does not already have a value.
            if os.environ.get(key) is None and value:
                os.environ[key] = value


def load_secrets() -> None:
    """Load secrets from the local .env file into os.environ."""
    for path in (ROOT_ENV, ALT_ENV):
        _load_env_file(path)
    if os.environ.get("DEEPSEEK_API_KEY") is None and DEEPSEEK_SECRET.exists():
        value = DEEPSEEK_SECRET.read_text(encoding="utf-8-sig").strip()
        if value:
            os.environ["DEEPSEEK_API_KEY"] = value
    # DeepSeek exposes an Anthropic-compatible endpoint in this project. Keep
    # the provider-facing name intuitive while preserving existing callers.
    if os.environ.get("DEEPSEEK_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]


# Auto-load on import so scripts get keys immediately.
load_secrets()
