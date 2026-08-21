import argparse
import asyncio
import logging
import sys

from reddit_mcp.application.tools import DependencyContainer
from reddit_mcp.infrastructure.logging import setup_logging
from reddit_mcp.interface.server import create_server


def main():
    """
    Main entry point for the Reddit MCP Server.

    This script initializes the appropriate logging configuration,
    sets up the server, and starts the selected transport (STDIO or SSE).
    """
    parser = argparse.ArgumentParser(
        description="Reddit MCP Server (AI-Native Edition)"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode to use (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind for SSE transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind for SSE transport (default: 8000)",
    )
    args = parser.parse_args()

    # 1. Initialize logging based on transport mode
    setup_logging(transport_mode=args.transport, level=logging.INFO)
    logger = logging.getLogger(__name__)

    try:
        logger.info(f"Starting Reddit MCP Server in {args.transport.upper()} mode...")

        # 2. Create the FastMCP server instance
        mcp = create_server()

        # 3. Start the selected transport loop.
        if args.transport == "sse":
            logger.info(f"Starting SSE transport on http://{args.host}:{args.port}")
            mcp.run(transport="sse", host=args.host, port=args.port)
        else:
            logger.info(
                "Starting STDIO transport loop. Listening for JSON-RPC messages."
            )
            mcp.run(transport="stdio")

    except KeyboardInterrupt:
        logger.info("Server stopped by user.")
    except Exception:
        logger.exception("Fatal error encountered")
        sys.exit(1)
    finally:
        logger.info("Cleaning up resources...")
        try:
            asyncio.run(DependencyContainer.aclose())
        except RuntimeError as loop_error:
            # Expected if the ASGI server already closed the event loop where clients were attached
            if "Event loop is closed" not in str(
                loop_error
            ) and "different loop" not in str(loop_error):
                logger.error(f"Runtime error during cleanup: {loop_error}")
        except Exception as cleanup_error:  # noqa: BLE001
            logger.error(f"Error during cleanup: {cleanup_error}")


if __name__ == "__main__":
    main()
