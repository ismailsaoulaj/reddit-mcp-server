import os
import subprocess
import sys

import pytest

from reddit_mcp.infrastructure import settings as settings_module
from reddit_mcp.infrastructure.settings import AppConfig


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Isolate external configuration so the default UA factory is exercised."""
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _clear_install_id_caches()
    yield
    _clear_install_id_caches()


def _clear_install_id_caches():
    settings_module._install_id.cache_clear()
    settings_module._process_install_id.cache_clear()


def test_default_user_agent_is_stable_across_instances(isolated_env):
    config_a = AppConfig(_env_file=None)
    config_b = AppConfig(_env_file=None)

    assert config_a.reddit_user_agent == config_b.reddit_user_agent
    assert config_a.reddit_user_agent.startswith("reddit-mcp-server/")


def test_default_user_agent_persists_across_restart(isolated_env):
    first_run = AppConfig(_env_file=None).reddit_user_agent

    # Simulate a fresh process (new process ID) reading the persisted install ID.
    settings_module._process_install_id.cache_clear()
    settings_module._install_id.cache_clear()

    assert AppConfig(_env_file=None).reddit_user_agent == first_run


def test_default_user_agent_is_unique_per_install(isolated_env, monkeypatch, tmp_path):
    install_a = AppConfig(_env_file=None).reddit_user_agent

    # Fresh process with a separate state directory = a different install.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-2"))
    _clear_install_id_caches()

    install_b = AppConfig(_env_file=None).reddit_user_agent

    assert install_a != install_b


def test_default_user_agent_tolerates_unwritable_state_dir(
    isolated_env, monkeypatch, tmp_path
):
    # A file occupying the state path makes both read and mkdir fail; the
    # factory must fall back to the process-stable ID instead of crashing.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(
        settings_module, "_install_id_path", lambda: blocker / "install-id"
    )

    config_a = AppConfig(_env_file=None)
    config_b = AppConfig(_env_file=None)

    assert config_a.reddit_user_agent == config_b.reddit_user_agent
    assert config_a.reddit_user_agent.startswith("reddit-mcp-server/")


def test_global_config_env_file_support(monkeypatch, tmp_path):
    # Ensure global XDG config .env is read properly
    config_dir = tmp_path / "config" / "reddit-mcp-server"
    config_dir.mkdir(parents=True)
    env_file = config_dir / ".env"
    env_file.write_text("REDDIT_CLIENT_ID=xdg_client_id\n", encoding="utf-8")

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.chdir(tmp_path)

    config = AppConfig()
    assert config.reddit_client_id == "xdg_client_id"


def test_explicit_user_agent_wins():
    config = AppConfig(reddit_user_agent="custom-agent", _env_file=None)

    assert config.reddit_user_agent == "custom-agent"


def test_saved_rss_url_defaults_to_none(monkeypatch):
    monkeypatch.delenv("REDDIT_SAVED_RSS_URL", raising=False)
    config = AppConfig(_env_file=None)

    assert config.reddit_saved_rss_url is None


def test_saved_rss_url_env_override(monkeypatch):
    monkeypatch.setenv(
        "REDDIT_SAVED_RSS_URL", "https://old.reddit.com/saved.rss?feed=abc&user=x"
    )

    config = AppConfig(_env_file=None)

    assert config.reddit_saved_rss_url.endswith("feed=abc&user=x")


def test_concurrency_and_rate_limit_defaults():
    config = AppConfig(_env_file=None)
    assert config.reddit_max_concurrency == 4
    assert config.reddit_rate_limit_per_minute == 40
    assert config.reddit_session_cookie is None
    assert config.mcp_transport == "stdio"
    assert config.mcp_host == "127.0.0.1"
    assert config.mcp_port == 8000


def test_transport_settings_env_override(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9000")

    config = AppConfig(_env_file=None)
    assert config.mcp_transport == "sse"
    assert config.mcp_host == "0.0.0.0"
    assert config.mcp_port == 9000


def test_concurrency_and_cookie_env_override(monkeypatch):
    monkeypatch.setenv("REDDIT_MAX_CONCURRENCY", "8")
    monkeypatch.setenv("REDDIT_RATE_LIMIT_PER_MINUTE", "60")
    monkeypatch.setenv("REDDIT_SESSION_COOKIE", "secret_session_token")

    config = AppConfig(_env_file=None)
    assert config.reddit_max_concurrency == 8
    assert config.reddit_rate_limit_per_minute == 60
    assert config.reddit_session_cookie == "secret_session_token"


_PRINT_UA_SCRIPT = (
    "from reddit_mcp.infrastructure.settings import AppConfig; "
    "print(AppConfig(_env_file=None).reddit_user_agent)"
)
_CHILD_TIMEOUT_SECONDS = 30


def test_install_id_shared_across_concurrent_processes(tmp_path):
    # Several processes racing on their first start (shared XDG_STATE_HOME,
    # no install-id file yet) must converge on a single install ID.
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env.pop("REDDIT_USER_AGENT", None)

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _PRINT_UA_SCRIPT],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    outputs = [None] * len(procs)
    timed_out = []
    for index, proc in enumerate(procs):
        try:
            outputs[index] = proc.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out.append(index)

    if timed_out:
        # Kill and reap stragglers so no zombies/pipes survive the failure.
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
        for proc in procs:
            proc.wait()
        pytest.fail(
            f"child process(es) {timed_out} did not finish within "
            f"{_CHILD_TIMEOUT_SECONDS}s"
        )

    for proc, (_, stderr) in zip(procs, outputs):
        assert proc.returncode == 0, stderr

    user_agents = {stdout.strip() for stdout, _ in outputs}
    assert len(user_agents) == 1
    assert user_agents.pop().startswith("reddit-mcp-server/")
