import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from reddit_mcp.infrastructure.logging import setup_logging
from reddit_mcp.main import main


def test_setup_logging_stdio_uses_stderr():
    setup_logging(transport_mode="stdio")
    root_logger = logging.getLogger()
    assert len(root_logger.handlers) == 1
    assert root_logger.handlers[0].stream == sys.stderr


def test_setup_logging_sse_uses_stdout():
    setup_logging(transport_mode="sse")
    root_logger = logging.getLogger()
    assert len(root_logger.handlers) == 1
    assert root_logger.handlers[0].stream == sys.stdout

    setup_logging(transport_mode="http")
    assert root_logger.handlers[0].stream == sys.stdout

    # Reset back to default stdio logging for other tests
    setup_logging(transport_mode="stdio")


def test_main_runs_stdio_by_default():
    mock_mcp = MagicMock()
    with (
        patch("sys.argv", ["reddit-mcp"]),
        patch("reddit_mcp.main.create_server", return_value=mock_mcp),
        patch("reddit_mcp.main.setup_logging") as mock_log,
        patch(
            "reddit_mcp.main.DependencyContainer.aclose", new_callable=AsyncMock
        ) as mock_aclose,
    ):
        main()
        mock_log.assert_called_once_with(transport_mode="stdio", level=logging.INFO)
        mock_mcp.run.assert_called_once_with(transport="stdio")
        mock_aclose.assert_called_once()


def test_main_runs_sse_with_custom_flags():
    mock_mcp = MagicMock()
    test_args = [
        "reddit-mcp",
        "--transport",
        "sse",
        "--host",
        "0.0.0.0",
        "--port",
        "9090",
    ]
    with (
        patch("sys.argv", test_args),
        patch("reddit_mcp.main.create_server", return_value=mock_mcp),
        patch("reddit_mcp.main.setup_logging") as mock_log,
        patch(
            "reddit_mcp.main.DependencyContainer.aclose", new_callable=AsyncMock
        ) as mock_aclose,
    ):
        main()
        mock_log.assert_called_once_with(transport_mode="sse", level=logging.INFO)
        mock_mcp.run.assert_called_once_with(transport="sse", host="0.0.0.0", port=9090)
        mock_aclose.assert_called_once()


def test_main_runs_http_with_custom_flags():
    mock_mcp = MagicMock()
    test_args = [
        "reddit-mcp",
        "--transport",
        "http",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
    ]
    with (
        patch("sys.argv", test_args),
        patch("reddit_mcp.main.create_server", return_value=mock_mcp),
        patch("reddit_mcp.main.setup_logging") as mock_log,
        patch(
            "reddit_mcp.main.DependencyContainer.aclose", new_callable=AsyncMock
        ) as mock_aclose,
    ):
        main()
        mock_log.assert_called_once_with(transport_mode="http", level=logging.INFO)
        mock_mcp.run.assert_called_once_with(
            transport="http", host="0.0.0.0", port=8000
        )
        mock_aclose.assert_called_once()


def test_main_graceful_shutdown_on_keyboard_interrupt():
    mock_mcp = MagicMock()
    mock_mcp.run.side_effect = KeyboardInterrupt()
    with (
        patch("sys.argv", ["reddit-mcp"]),
        patch("reddit_mcp.main.create_server", return_value=mock_mcp),
        patch("reddit_mcp.main.setup_logging"),
        patch(
            "reddit_mcp.main.DependencyContainer.aclose", new_callable=AsyncMock
        ) as mock_aclose,
    ):
        main()
        mock_aclose.assert_called_once()
