import logging
import sys


def setup_logging(transport_mode: str = "stdio", level: int = logging.INFO) -> None:
    """
    Configures the root logger based on the transport mode.

    For 'stdio', logs strictly to stderr to prevent JSON-RPC corruption.
    For 'sse' (HTTP), logs to stdout for standard Docker/service compatibility.

    Args:
        transport_mode: The MCP transport mode ('stdio' or 'sse')
        level: The logging level to set (e.g., logging.INFO, logging.DEBUG)
    """
    # Choose the appropriate stream based on the transport mode
    stream = sys.stdout if transport_mode.lower() in ("sse", "http") else sys.stderr
    handler = logging.StreamHandler(stream)

    # Define a clear format for the logs
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    handler.setFormatter(formatter)

    # Get the root logger
    root_logger = logging.getLogger()

    # Remove any existing handlers to prevent duplicate logs or stdout leakage
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    # Ensure our own package logger is also explicitly set
    logger = logging.getLogger("reddit_mcp")
    logger.setLevel(level)

    logger.debug("Logging initialized (stderr only).")
