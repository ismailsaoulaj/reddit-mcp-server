import os
import time
import uuid
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    _VERSION = version("reddit-mcp-server")
except PackageNotFoundError:
    try:
        _VERSION = version("reddit-mcp-ai")
    except PackageNotFoundError:
        _VERSION = "dev"

_INSTALL_ID_LENGTH = 8
_HEX_DIGITS = set("0123456789abcdef")
_ADOPT_TIMEOUT_SECONDS = 1.0


@lru_cache(maxsize=1)
def _process_install_id() -> str:
    """One ID per process, used both for persisting and as the in-memory
    fallback when the state directory is unreadable or unwritable (e.g.
    locked-down containers); never regenerated per instance."""
    return uuid.uuid4().hex[:_INSTALL_ID_LENGTH]


def _install_id_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "reddit-mcp-server" / "install-id"


def _is_valid_install_id(value: str) -> bool:
    return len(value) == _INSTALL_ID_LENGTH and set(value) <= _HEX_DIGITS


def _adopt_install_id(path: Path) -> str:
    """Wait out the process that won exclusive creation, then adopt its ID.
    A corrupt or stuck file is atomically self-healed with our own ID."""
    deadline = time.monotonic() + _ADOPT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError:
            existing = ""
        if existing:
            if _is_valid_install_id(existing):
                return existing
            break  # non-empty but corrupt; self-heal below
        time.sleep(0.02)
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(_process_install_id(), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass
    return _process_install_id()


@lru_cache(maxsize=1)
def _install_id() -> str:
    """Per-install identifier persisted in the user's state directory so the
    default User-Agent stays stable across process restarts. Creation is
    exclusive (O_CREAT|O_EXCL) so concurrent first-starts converge on one ID:
    losers reread and adopt the winner's value."""
    try:
        path = _install_id_path()
    except (OSError, RuntimeError):
        # No determinable home directory (e.g. minimal containers).
        return _process_install_id()
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if _is_valid_install_id(existing):
            return existing
    except OSError:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return _process_install_id()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return _adopt_install_id(path)
    except OSError:
        return _process_install_id()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_process_install_id())
    except OSError:
        return _process_install_id()
    return _process_install_id()


def _global_env_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "reddit-mcp-server" / ".env"


def _default_user_agent() -> str:
    return (
        f"reddit-mcp-server/{_VERSION} "
        f"(by /u/reddit-mcp-server-dev; install:{_install_id()})"
    )


class AppConfig(BaseSettings):
    """
    Centralized configuration for the Reddit MCP Server.
    Validates environment variables at startup (Fail-Fast).
    """

    reddit_client_id: str | None = Field(
        default=None, description="Reddit App Client ID"
    )
    reddit_client_secret: str | None = Field(
        default=None, description="Reddit App Client Secret"
    )
    reddit_user_agent: str = Field(
        default_factory=_default_user_agent,
        description="User-Agent string for HTTP requests",
    )
    reddit_saved_rss_url: str | None = Field(
        default=None,
        description=(
            "Private saved-items feed URL "
            "Found at https://www.reddit.com/prefs/feeds/ ('your saved links'). "
            "The feed token is the credential — treat it like a password."
        ),
    )
    reddit_session_cookie: str | None = Field(
        default=None,
        description=(
            "Optional: reddit_session cookie for authenticated requests. "
            "⚠️ Use a secondary/alt account only. Never use your main account. "
            "Extract from browser DevTools -> Application -> Cookies -> reddit.com."
        ),
    )

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def __init__(self, **values):
        if "_env_file" not in values:
            values["_env_file"] = (_global_env_path(), ".env")
        super().__init__(**values)


@lru_cache
def get_settings() -> AppConfig:
    """
    Returns a cached instance of the application settings.
    Will raise a ValidationError immediately if required vars are missing.
    """
    return AppConfig()
