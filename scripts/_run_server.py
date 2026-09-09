"""Start the web2api server with graceful shutdown.

Usage:
    python scripts/_run_server.py [--host HOST] [--port PORT]

Press Ctrl+C to gracefully shut down.
"""

import argparse
import logging
import signal
import socket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("web2api.startup")


def main() -> None:
    import uvicorn
    from cuhk_shenzhen_web2api.server import app

    parser = argparse.ArgumentParser(description="Start web2api server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=True,
        timeout_keep_alive=30,
    )
    server = uvicorn.Server(config)

    def shutdown(sig, _frame):
        log.info("Received %s, shutting down gracefully...", signal.Signals(sig).name)
        server.should_exit = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Starting server on %s:%d (Ctrl+C to stop)", args.host, args.port)
    try:
        server.run()
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        log.info("Server stopped.")


if __name__ == "__main__":
    main()
