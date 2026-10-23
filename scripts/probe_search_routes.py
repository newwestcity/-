"""Read routing metadata without calling a model or printing service bodies."""
from pathlib import Path
import json
import os
import re
import sys
from urllib import error, request
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest.provider import _NoRedirects, validate_configuration

SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}\Z")


def notice(label, data):
    value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    secret = os.environ.get("DIGEST_API_KEY")
    if secret:
        value = value.replace(secret, "[redacted]")
    print("::notice title=" + label + "::" + value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def safe_list(value):
    if isinstance(value, dict):
        value = list(value)
    return [v for v in value if isinstance(v, str) and SAFE.fullmatch(v)][:100] if isinstance(value, list) else []


def main():
    base = os.environ["DIGEST_API_BASE"]
    key = os.environ["DIGEST_API_KEY"]
    validate_configuration(base_url=base, api_key=key, model="route-probe")
    parts = urlsplit(base)
    origin = parts.scheme + "://" + parts.netloc
    output = {}
    for label, url, auth in (("models", base.rstrip("/") + "/models", True),
                             ("pricing", origin + "/api/pricing", False),
                             ("status", origin + "/api/status", False)):
        headers = {"Accept": "application/json", "User-Agent": "reading-digest-route-probe"}
        if auth:
            headers["Authorization"] = "Bearer " + key
        try:
            with request.build_opener(_NoRedirects()).open(request.Request(url, headers=headers), timeout=25) as response:
                status = response.status
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            data = json.loads(raw)
            entries = data.get("data") if isinstance(data, dict) else None
            report = {"http_status": status, "model_count": len(entries) if isinstance(entries, list) else 0}
            if isinstance(entries, list):
                rows = []
                for item in entries[:1000]:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("id") if label == "models" else item.get("model_name")
                    if not isinstance(name, str) or not SAFE.fullmatch(name):
                        continue
                    if not re.search(r"gpt|glm|gemini|sonar|qwen|search", name, re.I):
                        continue
                    rows.append({"model": name,
                                 "endpoints": safe_list(item.get("supported_endpoint_types", [])),
                                 "groups": safe_list(item.get("enable_groups", []))})
                report["models"] = rows[:200]
            elif isinstance(entries, dict):
                report["metadata_keys"] = safe_list(list(entries))
            output[label] = report
            for i in range(0, len(report.get("models", [])), 20):
                notice("Search route directory", {"mode": label, "models": report["models"][i:i + 20]})
            notice("Search route metadata", {k: v for k, v in report.items() if k != "models"} | {"mode": label})
        except error.HTTPError as exc:
            output[label] = {"http_status": exc.code}
            exc.close()
            notice("Search route metadata", {"mode": label, **output[label]})
        except Exception:
            output[label] = {"read_failed": True}
            notice("Search route metadata", {"mode": label, "read_failed": True})
    Path(".local").mkdir(exist_ok=True)
    Path(".local/search-route-probe.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
