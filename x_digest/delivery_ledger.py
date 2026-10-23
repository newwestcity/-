"""Durable, compare-and-swap delivery reservations in a dedicated Git branch.

The ledger contains only edition dates and public item identities. A pending
reservation is deliberately *not* retried after an ambiguous SMTP outcome.
GitHub Cache is used only for the first migration, never as authority after it.
"""

from __future__ import annotations

import base64
from datetime import date as Date
from http.client import HTTPException
import json
import os
import re
import ssl
from urllib import error, request

from . import reading_state


BRANCH = "digest-delivery-state"
LEDGER_PATH = "reading-ledger.json"
MAX_BYTES = 2 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class LedgerError(ValueError):
    """A fixed, credential-free diagnostic suitable for Actions logs."""


class _NoRedirects(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reading_state._unique_json_object)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise LedgerError("持久发送账本数据损坏；已停止自动投递") from None


def _validate(data):
    if (not isinstance(data, dict) or set(data) != {"version", "history", "pending"}
            or type(data["version"]) is not int or data["version"] != 1
            or not isinstance(data["pending"], list) or len(data["pending"]) > 1000):
        raise LedgerError("持久发送账本格式无效；已停止自动投递")
    try:
        history = reading_state._validated_state(data["history"])
        # Pending entries use exactly the same bounded record schema as history.
        pending = reading_state._validated_state({"version": 2, "deliveries": data["pending"]})["deliveries"]
        if any(entry["edition"] == "legacy-x" for entry in pending):
            raise ValueError
        completed = {(entry["edition"], entry["period"]) for entry in history["deliveries"]}
        if any((entry["edition"], entry["period"]) in completed for entry in pending):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise LedgerError("持久发送账本记录无效；已停止自动投递") from None
    return {"version": 1, "history": history, "pending": pending}


def _encode(data):
    raw = (json.dumps(_validate(data), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise LedgerError("持久发送账本超出大小限制；已停止自动投递")
    return raw


class Ledger:
    def __init__(self, repository, token):
        if (not isinstance(repository, str) or not _REPO.fullmatch(repository)
                or any(part in {".", ".."} for part in repository.split("/"))
                or not isinstance(token, str) or not token.strip()
                or any(char.isspace() for char in token)):
            raise LedgerError("缺少持久发送账本的GitHub配置；已停止自动投递")
        self._base = "https://api.github.com/repos/" + repository
        self._token = token
        self._data = None
        self._sha = None
        self._reservation = None

    def _api(self, path, *, method="GET", payload=None, missing=False):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(self._base + path, data=body, method=method, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "reading-digest-delivery-ledger",
        })
        try:
            opener = request.build_opener(_NoRedirects(), request.HTTPSHandler(context=ssl.create_default_context()))
            with opener.open(req, timeout=30) as response:
                raw = response.read(MAX_BYTES * 2 + 1)
            if len(raw) > MAX_BYTES * 2:
                raise LedgerError("持久发送账本响应超出限制；已停止自动投递")
            return _json(raw)
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            if missing and status == 404:
                return None
            if status in {409, 422}:
                raise LedgerError("持久发送账本发生并发更新；请核对记录后重试") from None
            if status in {401, 403}:
                raise LedgerError("持久发送账本读取或写入权限不足；已停止自动投递") from None
            raise LedgerError("持久发送账本服务不可用；已停止自动投递") from None
        except (error.URLError, OSError, HTTPException, ValueError, TypeError) as exc:
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("持久发送账本连接失败；已停止自动投递") from None

    @property
    def history(self):
        # Return a validated copy so the caller cannot mutate pending authority.
        return reading_state._validated_state(self._data["history"])

    def _read(self):
        response = self._api("/contents/" + LEDGER_PATH + "?ref=" + BRANCH)
        try:
            if (response.get("type") != "file" or response.get("encoding") != "base64"
                    or not _SHA.fullmatch(response["sha"]) or not isinstance(response["content"], str)):
                raise ValueError
            encoded = "".join(response["content"].split())
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > MAX_BYTES:
                raise ValueError
            data = _validate(_json(raw))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise LedgerError("持久发送账本文件无效；已停止自动投递") from None
        self._data, self._sha = data, response["sha"]

    def _write(self, data):
        raw = _encode(data)
        response = self._api("/contents/" + LEDGER_PATH, method="PUT", payload={
            "message": "Update reading delivery ledger", "branch": BRANCH, "sha": self._sha,
            "content": base64.b64encode(raw).decode("ascii"),
        })
        try:
            sha = response["content"]["sha"]
            if not _SHA.fullmatch(sha):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            # The update may already have succeeded: never automatically retry.
            raise LedgerError("持久发送账本更新结果未确认；请核对记录") from None
        self._data, self._sha = _validate(data), sha

    def pending_ids(self, *, today, history_days=90):
        reading_state._validate_history_days(history_days)
        if type(today) is not Date:
            raise LedgerError("持久发送账本日期配置无效")
        cutoff = reading_state._cutoff(today, history_days)
        return {item for entry in self._data["pending"]
                if cutoff <= Date.fromisoformat(entry["date"]) <= today for item in entry["item_ids"]}

    def check_available(self, *, edition, period):
        """Reject ambiguous prior sends before paid search or rendering work."""
        try:
            reading_state._validate_public_period(edition, period)
        except (ValueError, TypeError):
            raise LedgerError("发送版次或周期无效；已停止自动投递") from None
        if any(row["edition"] == edition and row["period"] == period for row in self._data["pending"]):
            raise LedgerError("本期已有未确认的SMTP发送记录；请核对收件箱，禁止自动重发")

    def reserve(self, *, edition, period, date, item_ids, sent_at):
        if self._reservation is not None:
            raise LedgerError("本次运行已有发送预留记录；已停止重复投递")
        try:
            reading_state._validate_public_period(edition, period)
            entry = reading_state._validated_state({"version": 2, "deliveries": [{
                "edition": edition, "period": period, "date": date,
                "item_ids": item_ids, "sent_at": sent_at,
            }]})["deliveries"][0]
        except (ValueError, TypeError, KeyError):
            raise LedgerError("发送预留记录无效；已停止自动投递") from None
        if reading_state.already_sent(self.history, edition=edition, period=period):
            raise LedgerError("本期已记录发送；已停止重复投递")
        self.check_available(edition=edition, period=period)
        cutoff = reading_state._cutoff(Date.fromisoformat(date), 180)
        pending = [row for row in self._data["pending"] if Date.fromisoformat(row["date"]) >= cutoff]
        updated = {**self._data, "pending": pending + [entry]}
        self._write(updated)
        self._reservation = (edition, period)

    def complete(self, history):
        if self._reservation is None:
            raise LedgerError("缺少本次发送预留记录；不能确认投递")
        try:
            clean = reading_state._validated_state(history)
        except (ValueError, TypeError):
            raise LedgerError("发送成功记录无效；请核对投递结果") from None
        edition, period = self._reservation
        reserved = next(row for row in self._data["pending"]
                        if (row["edition"], row["period"]) == self._reservation)
        completed = next((row for row in clean["deliveries"]
                          if (row["edition"], row["period"]) == self._reservation), None)
        if completed != reserved:
            raise LedgerError("发送成功记录与预留不一致；请核对投递结果")
        old = {(row["edition"], row["period"]): row for row in self.history["deliveries"]}
        cutoff = reading_state._cutoff(Date.fromisoformat(reserved["date"]), 180)
        current = {(row["edition"], row["period"]): row for row in clean["deliveries"]}
        if any(current.get(key) != row for key, row in old.items() if Date.fromisoformat(row["date"]) >= cutoff):
            raise LedgerError("发送成功记录丢失已有历史；已保留预留记录")
        pending = [row for row in self._data["pending"] if (row["edition"], row["period"]) != self._reservation]
        self._write({"version": 1, "history": clean, "pending": pending})
        self._reservation = None

    def cancel(self):
        """Release only a definitely rejected send, never a timeout/disconnect."""
        if self._reservation is None:
            return
        pending = [row for row in self._data["pending"] if (row["edition"], row["period"]) != self._reservation]
        self._write({**self._data, "pending": pending})
        self._reservation = None


def open_ledger(initial_history, *, allow_create=True):
    """Read durable authority or atomically create its initial orphan branch."""
    if type(allow_create) is not bool:
        raise LedgerError("持久发送账本初始化配置无效；已停止自动投递")
    ledger = Ledger(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GH_TOKEN", ""))
    ref = ledger._api("/git/ref/heads/" + BRANCH, missing=True)
    if ref is None:
        if not allow_create:
            raise LedgerError("持久发送账本不存在；已停止投递，禁止自动重建空历史")
        try:
            history = initial_history() if callable(initial_history) else initial_history
        except (ValueError, OSError, TypeError, KeyError):
            raise LedgerError("首次迁移的发送历史读取失败；已停止自动投递") from None
        initial = {"version": 1, "history": history, "pending": []}
        content = _encode(initial).decode("utf-8")
        try:
            tree = ledger._api("/git/trees", method="POST", payload={"tree": [{
                "path": LEDGER_PATH, "mode": "100644", "type": "blob", "content": content,
            }]})["sha"]
            if not _SHA.fullmatch(tree):
                raise ValueError
            commit = ledger._api("/git/commits", method="POST", payload={
                "message": "Initialize reading delivery ledger", "tree": tree, "parents": [],
            })["sha"]
            if not _SHA.fullmatch(commit):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("持久发送账本初始化结果无效；已停止自动投递") from None
        ledger._api("/git/refs", method="POST", payload={"ref": "refs/heads/" + BRANCH, "sha": commit})
    ledger._read()
    return ledger
