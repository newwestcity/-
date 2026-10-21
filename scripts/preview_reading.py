"""Produce inspectable, sanitized release previews without delivering mail."""
from collections import Counter
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest.app import ConfigurationError, load_settings
from x_digest.provider import ProviderError
from x_digest.reading import run_reading


def notice(title, value):
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    print("::notice title=" + title + "::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def main():
    edition = os.environ.get("EDITION", "daily")
    try:
        result = run_reading(load_settings(Path("settings.json")), work_dir=Path(".local"), send=False, edition=edition)
    except (ConfigurationError, ProviderError):
        result = {"status": "no_preview", "edition": edition}
    notice("Reading preview result", {**result, "mail_sent": False})
    path = Path(".local/reading-audit.json")
    if path.exists():
        audit = json.loads(path.read_text(encoding="utf-8"))
        notice("Reading source diagnostics", audit)
    if result["status"] != "preview":
        return 1
    data = json.loads(Path(".local/preview/digest.json").read_text(encoding="utf-8"))
    counts = Counter(item["source"] for item in data["items"])
    notice("Reading verified sources", dict(counts))
    for item in data["items"][:7]:
        public = {key: item[key] for key in ("source", "url", "title", "summary", "reason", "score_total")}
        if edition == "weekly":
            public.update(learning_points=item["learning_points"], reproduction_steps=item["reproduction_steps"])
        notice("Reading example", public)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
