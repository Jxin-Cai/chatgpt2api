"""Public text model policy, pinned after the 2026-09-30 catalog review."""

SUPPORTED_TEXT_MODELS = (
    "gpt-6-pro",
    "gpt-5.6-instant",
    "gpt-5.6-thinking",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
)
DEFAULT_TEXT_MODEL = "gpt-6.1-sol"

# Ordinary Chat models use native, non-wm slugs. A shared model family does
# not make Chat Pro interchangeable with the Work Mode route.
CHAT_TEXT_MODELS = frozenset({"gpt-6-pro", "gpt-5.6-instant", "gpt-5.6-thinking"})


def web_thinking_effort(value: str) -> str:
    """Approximate API effort levels using the current Web catalog's tiers."""
    return {
        "minimal": "min", "low": "min", "min": "min",
        "medium": "standard", "standard": "standard",
        "high": "extended", "extended": "extended",
        "xhigh": "max", "max": "max",
    }.get(str(value or "").strip().lower(), "")
