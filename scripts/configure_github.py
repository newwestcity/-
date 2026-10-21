"""Configure Actions without putting raw secrets in arguments, logs or files.

The default invocation only previews non-sensitive settings. Network access and
writes require --apply. Cloud proxy placeholders are deliberately never copied
from process secret variables into GitHub Secrets.
"""

from __future__ import annotations

import argparse
from email.headerregistry import Address
import getpass
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit
import warnings


DEFAULT_REPO = "newwestcity/-"
SECRET_NAMES = ("DIGEST_API_KEY", "SMTP_AUTH_CODE")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class ConfigureError(ValueError):
    """A safe error message that never includes gh output or raw credentials."""


def _gh(arguments: list[str], *, operation: str, secret: str | None = None) -> str:
    try:
        completed = subprocess.run(
            ["gh", *arguments], input=secret, text=True, capture_output=True,
            check=False, timeout=45,
        )
    except FileNotFoundError:
        raise ConfigureError("未找到 GitHub CLI（gh）") from None
    except subprocess.TimeoutExpired:
        raise ConfigureError(f"{operation}超时；未输出服务响应") from None
    except OSError:
        raise ConfigureError(f"{operation}无法执行；未输出服务响应") from None
    if completed.returncode:
        raise ConfigureError(f"{operation}失败（退出码 {completed.returncode}）；未输出服务响应")
    return completed.stdout


def _mailbox(value: str) -> str:
    if not isinstance(value, str) or "\r" in value or "\n" in value:
        raise ConfigureError("邮箱地址格式无效")
    try:
        address = Address(addr_spec=value)
    except (ValueError, IndexError):
        raise ConfigureError("邮箱地址格式无效") from None
    if not address.username or not address.domain:
        raise ConfigureError("邮箱地址格式无效")
    return str(address)


def _api_base(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or any(char.isspace() for char in value)
            or not parsed.path.rstrip("/").endswith("/v1")
        ):
            raise ValueError
        parsed.port
    except ValueError:
        raise ConfigureError("API 地址必须是已核实且不含凭据的 HTTPS /v1 地址") from None
    return value.rstrip("/")


def _model(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    if any(char in value for char in "\r\n"):
        raise ConfigureError("模型名称格式无效")
    return value.strip()


def _prompt_secret(name: str) -> str:
    try:
        with warnings.catch_warnings():
            # getpass normally falls back to an echoed stdin if a terminal is
            # unavailable. Refuse that fallback before it can read a secret.
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(f"{name}（隐藏输入；留空保留现有值）：")
    except getpass.GetPassWarning:
        raise ConfigureError("当前终端无法隐藏输入；请改用安全终端或 GitHub 密钥页面") from None
    except (EOFError, KeyboardInterrupt):
        raise ConfigureError("已取消安全输入，尚未写入 GitHub") from None
    if "\r" in value or "\n" in value:
        raise ConfigureError(f"{name} 输入包含换行，尚未写入 GitHub")
    return value


def configure(
    *, repo: str = DEFAULT_REPO, mailbox: str | None = None,
    api_base: str | None = None, model: str | None = None,
    apply: bool = False, prompt_secrets: bool = False,
) -> int:
    if not _REPO.fullmatch(repo) or any(part in {".", ".."} for part in repo.split("/")):
        raise ConfigureError("仓库必须使用 owner/repository 格式")
    if prompt_secrets and not apply:
        raise ConfigureError("--prompt-secrets 必须与 --apply 一起使用")
    mailbox = mailbox if mailbox is not None else os.environ.get("SMTP_USERNAME")
    if mailbox:
        mailbox = _mailbox(mailbox)
    elif apply:
        raise ConfigureError("缺少 SMTP_USERNAME；请设置该非敏感环境变量或使用 --email")
    # Only non-secret settings may be sourced from the process environment.
    api_base = _api_base(api_base if api_base is not None else os.environ.get("DIGEST_API_BASE"))
    model = _model(model if model is not None else os.environ.get("DIGEST_MODEL"))
    variables = {"SMTP_USERNAME": mailbox, "MAIL_TO": mailbox} if mailbox else {}
    if api_base is not None:
        variables["DIGEST_API_BASE"] = api_base
    if model is not None:
        variables["DIGEST_MODEL"] = model

    print(f"目标仓库：{repo}")
    if not mailbox:
        print("缺少 SMTP_USERNAME/MAIL_TO：请设置 SMTP_USERNAME 或使用 --email")
    for name, value in variables.items():
        print(f"{name} = {value}")
    for name, value in (("DIGEST_API_BASE", api_base), ("DIGEST_MODEL", model)):
        if value is None:
            print(f"{name} 未提供，将保留远端现有值")
    print("DIGEST_ENABLED：保留远端原值；未设置时不创建，沿用工作流默认行为")
    print("密钥只接受隐藏输入；不会从云环境复制任何密钥或占位符。")
    if not apply:
        print("预览完成：未联网、未写入。使用 --apply 才执行配置。")
        return 0

    # Finish all read-only permission checks before writes or secret prompts.
    _gh(["api", f"repos/{repo}", "--jq", ".full_name"], operation="仓库访问预检")
    enabled_output = _gh(
        ["api", "--paginate", f"repos/{repo}/actions/variables", "--jq",
         '.variables[] | select(.name == "DIGEST_ENABLED") | .value'],
        operation="变量读取预检",
    )
    existing_secrets = set(_gh(
        ["api", "--paginate", f"repos/{repo}/actions/secrets", "--jq", ".secrets[].name"],
        operation="密钥名称读取预检",
    ).splitlines())
    if enabled_output.strip():
        print("远端定时开关已有配置，将保留原值。")
    else:
        print("未创建定时开关变量，沿用工作流默认行为。")

    secrets_to_set = {}
    if prompt_secrets:
        for name in SECRET_NAMES:
            value = _prompt_secret(name)
            if value:
                secrets_to_set[name] = value

    for name, value in variables.items():
        _gh(["variable", "set", name, "--repo", repo, "--body", value],
            operation=f"配置变量 {name}")
        print(f"已配置变量：{name}")
    for name, value in secrets_to_set.items():
        _gh(["secret", "set", name, "--repo", repo], operation=f"配置密钥 {name}", secret=value)
        print(f"已配置密钥：{name}")
    missing = [name for name in SECRET_NAMES if name not in existing_secrets and name not in secrets_to_set]
    if missing:
        print("仍缺少安全配置：" + ", ".join(missing))
    else:
        print("两项密钥名称均已配置；实际接口和投递仍需验证。")
    print("GitHub 配置步骤完成；本操作未运行搜索、未发信，也未修改定时开关。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安全预览或填写每日精选的 GitHub Actions 配置")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--email", dest="mailbox", help="收发邮箱；未提供时使用 SMTP_USERNAME")
    parser.add_argument("--api-base", help="已核实的 HTTPS /v1 根地址；未提供时保留远端值")
    parser.add_argument("--model", help="已核实的模型名；未提供时保留远端值")
    parser.add_argument("--apply", action="store_true", help="预检通过后填写 GitHub 配置")
    parser.add_argument("--prompt-secrets", action="store_true", help="通过隐藏输入安全填写原始密钥")
    args = parser.parse_args(argv)
    try:
        return configure(repo=args.repo, mailbox=args.mailbox, api_base=args.api_base,
                         model=args.model, apply=args.apply, prompt_secrets=args.prompt_secrets)
    except ConfigureError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
