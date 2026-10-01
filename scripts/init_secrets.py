"""Replace sample local tokens with random values without printing them."""
from pathlib import Path
import secrets

path = Path(__file__).resolve().parent.parent / ".env"
text = path.read_text(encoding="utf-8")
text = text.replace("API_TOKEN=change-me-long-random-token", f"API_TOKEN={secrets.token_urlsafe(36)}")
text = text.replace("WEBHOOK_TOKEN=change-me-webhook-token", f"WEBHOOK_TOKEN={secrets.token_urlsafe(36)}")
path.write_text(text, encoding="utf-8")
