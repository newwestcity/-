import base64
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from threading import Lock
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from x_digest import github_projects as github


NOW = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
TOKEN = "synthetic-github-test-only-token"
README = """# Agent learning tool

This open source application demonstrates a practical automation workflow.
It keeps its integrations in small readable modules, with examples for local
development. The commands below are documentation and are never executed by
the recommendation service.

## Quickstart
```bash
pip install example-agent-tool
python -m example_agent_tool
```

## Examples
See [sample workflows](examples/README.md) and adapt them to your own inputs.

## Testing
```bash
pytest
```

## Documentation
Read the [development guide](docs/development.md) for design and extension notes.
"""


def metadata(name="Example/agent-tool", **overrides):
    value = {"full_name": name, "html_url": "https://github.com/" + name,
             "owner": {"login": name.split("/")[0]}, "private": False, "archived": False,
             "fork": False, "stargazers_count": 500, "forks_count": 30,
             "description": "An AI automation learning application", "topics": ["llm", "automation"],
             "language": "Python", "created_at": (NOW - timedelta(days=20)).isoformat(),
             "pushed_at": (NOW - timedelta(days=1)).isoformat(),
             "license": {"spdx_id": "MIT", "name": "MIT License"}}
    value.update(overrides)
    return value


def readme(name="Example/agent-tool", text=README):
    raw = text.encode()
    return {"encoding": "base64", "size": len(raw),
            "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
            "content": base64.encodebytes(raw).decode(),
            "html_url": "https://github.com/" + name + "/blob/main/README.md"}


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class GitHubProjectTests(unittest.TestCase):
    def service(self, repositories=None, readmes=None):
        repositories = [metadata()] if repositories is None else repositories
        readmes = {} if readmes is None else readmes
        opener = Mock()

        def opened(req, **kwargs):
            path = urlsplit(req.full_url).path
            if path == "/search/repositories":
                value = {"items": repositories, "incomplete_results": False}
            else:
                name = path[len("/repos/"):-len("/readme")]
                value = readmes.get(name, readme(name))
            return Response(json.dumps(value).encode())

        opener.open.side_effect = opened
        return opener

    def test_official_metadata_and_readme_are_grounded_without_executing_commands(self):
        opener = self.service()
        with patch.object(github.request, "build_opener", return_value=opener), \
             patch("subprocess.run", side_effect=AssertionError("must not execute README")):
            result = github.fetch_projects(now=NOW, token=TOKEN)
        self.assertEqual(len(result.projects), 1)
        project = result.projects[0]
        self.assertEqual(project["id"], "github:example/agent-tool")
        self.assertEqual(project["license"], "MIT")
        self.assertTrue(project["has_quickstart"])
        self.assertTrue(project["has_examples"])
        self.assertTrue(project["has_tests"])
        self.assertTrue(project["has_docs"])
        self.assertIn("Quickstart", " ".join(project["readme_evidence"]["quickstart"]))
        self.assertIn("未实际安装或运行", project["readiness_basis"])
        self.assertIsNone(project["weekly_star_growth"])
        self.assertEqual(result.diagnostics["api_requests"], 4)
        for call in opener.open.call_args_list:
            req = call.args[0]
            self.assertEqual(urlsplit(req.full_url).hostname, "api.github.com")
            self.assertEqual(req.get_header("Authorization"), "Bearer " + TOKEN)
            self.assertEqual(req.get_method(), "GET")

    def test_archive_forks_missing_license_and_lists_never_consume_readme_budget(self):
        repositories = [metadata("Example/archived", archived=True),
                        metadata("Example/fork", fork=True), metadata("Example/private", private=True),
                        metadata("Example/unlicensed", license=None),
                        metadata("Example/awesome-agents"),
                        metadata("Example/stale", pushed_at=(NOW - timedelta(days=8)).isoformat())]
        opener = self.service(repositories)
        with patch.object(github.request, "build_opener", return_value=opener):
            result = github.fetch_projects(now=NOW)
        self.assertEqual(result.projects, [])
        self.assertEqual(opener.open.call_count, 3)
        self.assertEqual(result.diagnostics["readmes_requested"], 0)

    def test_repository_identity_and_future_dates_cannot_be_forged(self):
        values = [metadata(html_url="https://github.com/Other/agent-tool"),
                  metadata(owner={"login": "Other"}),
                  metadata(created_at=(NOW + timedelta(hours=1)).isoformat()),
                  metadata(stargazers_count=True), metadata("Example/../evil")]
        opener = self.service(values)
        with patch.object(github.request, "build_opener", return_value=opener):
            result = github.fetch_projects(now=NOW)
        self.assertEqual(result.projects, [])
        self.assertEqual(opener.open.call_count, 3)

    def test_readme_hash_and_documented_quickstart_are_required(self):
        malformed = readme()
        malformed["content"] = base64.b64encode(b"not the original blob").decode()
        for value in (malformed, readme(text="Pay for our private hosted platform. " * 20),
                      readme(text="## Quickstart\nSign up at the website.\n" + "Marketing copy. " * 30),
                      readme(text="small readme")):
            opener = self.service(readmes={"Example/agent-tool": value})
            with self.subTest(value=value), patch.object(github.request, "build_opener", return_value=opener):
                result = github.fetch_projects(now=NOW)
            self.assertEqual(result.projects, [])

    def test_no_placeholder_projects_when_search_has_no_candidates(self):
        opener = self.service([])
        with patch.object(github.request, "build_opener", return_value=opener):
            result = github.fetch_projects(now=NOW)
        self.assertEqual(result.projects, [])
        self.assertEqual(result.diagnostics["projects_verified"], 0)

    def test_documented_start_steps_support_emoji_headings_and_rst(self):
        for heading in ("## 🚀 Quick Start", "Installation\n============", "## 安装与使用"):
            value = readme(text=README.replace("## Quickstart", heading))
            opener = self.service(readmes={"Example/agent-tool": value})
            with self.subTest(heading=heading), patch.object(github.request, "build_opener", return_value=opener):
                result = github.fetch_projects(now=NOW)
            self.assertEqual(len(result.projects), 1)
            self.assertTrue(result.projects[0]["has_quickstart"])

    def test_maximum_budget_is_three_searches_twenty_readmes_and_two_in_parallel(self):
        opener = self.service([metadata(f"Example/agent-{index}") for index in range(30)])
        original = opener.open.side_effect
        active = maximum = 0
        lock = Lock()

        def delayed(req, **kwargs):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(.003)
                return original(req, **kwargs)
            finally:
                with lock:
                    active -= 1

        opener.open.side_effect = delayed
        with patch.object(github.request, "build_opener", return_value=opener):
            result = github.fetch_projects(now=NOW, limit=20)
        self.assertEqual(len(result.projects), 20)
        self.assertEqual(result.diagnostics["api_requests"], 23)
        self.assertEqual(result.diagnostics["readmes_requested"], 20)
        self.assertLessEqual(maximum, 2)

    def test_history_is_casefolded_and_excluded_before_readme_limit_with_candidate_dedup(self):
        repositories = [metadata("Example/Already-Sent", stargazers_count=50000),
                        metadata("Example/New-First", stargazers_count=2000),
                        metadata("example/new-first", stargazers_count=2000),
                        metadata("Example/New-Second", stargazers_count=1000)]
        opener = self.service(repositories)
        with patch.object(github.request, "build_opener", return_value=opener):
            result = github.fetch_projects(now=NOW, limit=2,
                                           seen_ids={"GITHUB:EXAMPLE/ALREADY-SENT", "github:Example/Already-Sent"})
        self.assertEqual({project["id"] for project in result.projects},
                         {"github:example/new-first", "github:example/new-second"})
        paths = [urlsplit(call.args[0].full_url).path for call in opener.open.call_args_list]
        readme_paths = [path for path in paths if path.endswith("/readme")]
        self.assertEqual(set(readme_paths), {"/repos/Example/New-First/readme", "/repos/Example/New-Second/readme"})
        self.assertEqual(len(readme_paths), 2)
        self.assertEqual(result.diagnostics["api_requests"], 5)
        self.assertEqual(result.diagnostics["excluded_previously_seen"], 1)

    def test_authentication_or_rate_errors_stop_further_requests_without_echo(self):
        for status, label in ((401, "credential_rejected"), (403, "rate_limited_or_forbidden"),
                              (429, "rate_limited_or_forbidden")):
            opener = Mock()
            opener.open.side_effect = HTTPError("https://api.github.com/" + TOKEN, status,
                                               TOKEN, {}, BytesIO(TOKEN.encode()))
            with self.subTest(status=status), patch.object(github.request, "build_opener", return_value=opener):
                result = github.fetch_projects(now=NOW, token=TOKEN)
            self.assertEqual(result.projects, [])
            self.assertEqual(result.diagnostics[label], 1)
            self.assertEqual(opener.open.call_count, 1)
            self.assertNotIn(TOKEN, json.dumps(result.diagnostics))

    def test_redirects_are_rejected_without_forwarding_credentials(self):
        handler = github._NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.test/"))
        opener = Mock()
        opener.open.side_effect = HTTPError("https://api.github.com/repos/Example/old/readme", 302,
                                           TOKEN, {"Location": "https://other.test/"}, BytesIO(TOKEN.encode()))
        with patch.object(github.request, "build_opener", return_value=opener):
            result = github.fetch_projects(now=NOW, token=TOKEN)
        self.assertEqual(result.projects, [])
        self.assertEqual(result.diagnostics["redirect_refused"], 3)
        self.assertNotIn(TOKEN, json.dumps(result.diagnostics))

    def test_invalid_configuration_stops_before_network(self):
        with patch.object(github.request, "build_opener") as build:
            for overrides in ({"limit": 21}, {"limit": True}, {"token": "bad\n" + TOKEN},
                              {"now": datetime(2026, 10, 21)}, {"previous_snapshot": {"stars": {}}},
                              {"seen_ids": ["github:example/tool"]}, {"seen_ids": {True}},
                              {"seen_ids": {"x:example/tool"}}, {"seen_ids": {"github:Example/.."}},
                              {"seen_ids": {"github:Example/tool/extra"}},
                              {"seen_ids": {"github:Example/tool/"}}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError) as raised:
                    github.fetch_projects(**{"now": NOW, **overrides})
                self.assertNotIn(TOKEN, str(raised.exception))
            build.assert_not_called()

    def test_canonical_repository_urls_do_not_accept_arbitrary_pages_or_hosts(self):
        self.assertEqual(github.canonical_repository_url("https://github.com/Example/tool/"),
                         "https://github.com/Example/tool")
        for url in ("http://github.com/Example/tool", "https://github.com.evil/Example/tool",
                    "https://user:pass@github.com/Example/tool", "https://github.com/Example/tool/issues",
                    "https://github.com/Example/tool?token=" + TOKEN, "https://github.com/Example/.."):
            with self.subTest(url=url), self.assertRaises(ValueError):
                github.canonical_repository_url(url)

    def test_growth_requires_a_real_comparable_baseline(self):
        project = {"id": "github:example/tool", "stars": 120}
        self.assertIsNone(github.compute_growth([project], None, now=NOW)[project["id"]]["weekly_star_growth"])
        baseline = github.build_snapshot([{**project, "stars": 100}], observed_at=NOW - timedelta(days=7))
        growth = github.compute_growth([project], baseline, now=NOW)[project["id"]]
        self.assertEqual(growth["weekly_star_growth"], 20)
        self.assertEqual(growth["snapshot_interval_days"], 7)
        baseline["observed_at"] = (NOW - timedelta(days=30)).isoformat()
        growth = github.compute_growth([project], baseline, now=NOW)[project["id"]]
        self.assertEqual(growth["star_change_since_snapshot"], 20)
        self.assertIsNone(growth["weekly_star_growth"])


if __name__ == "__main__":
    unittest.main()
