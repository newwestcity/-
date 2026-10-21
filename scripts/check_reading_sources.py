"""One bounded source-availability check; no AI or email calls."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from x_digest.github_projects import fetch_projects
from x_digest.xueqiu import fetch_articles


def main():
    now = datetime.now(timezone.utc)
    github = fetch_projects(now=now, limit=3, token=os.environ.get("GH_TOKEN") or None)
    snow = fetch_articles(["https://xueqiu.com/1784314127/367934699"], now=now, limit=1)
    report = {"github": {"verified_projects": len(github.projects), "diagnostics": github.diagnostics},
              "xueqiu": {"verified_articles": len(snow.articles), "diagnostics": snow.diagnostics},
              "model_called": False, "mail_sent": False}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write("```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n")
    return 0 if github.projects and snow.articles else 1


if __name__ == "__main__":
    raise SystemExit(main())
