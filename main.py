from __future__ import annotations

import argparse

import uvicorn
from api import create_app
from services.realtime.server_protocol import RealtimeWebSocketProtocol

app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--access-log", action="store_true")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, access_log=args.access_log,
                log_level="info", ws=RealtimeWebSocketProtocol)
