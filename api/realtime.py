from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from api.support import require_identity
from services.account_service import account_service
from services.realtime.session import RealtimeSession
from utils.log import logger


def create_router() -> APIRouter:
    router = APIRouter()

    @router.websocket("/v1/realtime")
    async def realtime_endpoint(
        websocket: WebSocket,
        model: str = Query(default="gpt-4o-realtime-preview"),
    ):
        # 认证：从 header 或 query 中获取 token
        auth = (
            websocket.headers.get("authorization")
            or websocket.query_params.get("authorization")
        )
        # 支持 OpenAI 的 subprotocol 方式传递 token
        if not auth:
            for proto in websocket.headers.get("sec-websocket-protocol", "").split(","):
                proto = proto.strip()
                if proto.startswith("openai-insecure-api-key."):
                    auth = f"Bearer {proto.removeprefix('openai-insecure-api-key.')}"
                    break

        try:
            identity = require_identity(auth)
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        # 获取 ChatGPT access_token
        try:
            access_token = account_service.get_realtime_access_token()
        except RuntimeError as e:
            await websocket.accept()
            import json
            await websocket.send_text(json.dumps({
                "type": "error",
                "error": {"message": str(e), "code": "no_account"}
            }))
            await websocket.close(code=1011)
            return

        await websocket.accept()
        logger.info(f"[realtime] New session: model={model}, identity={identity.get('name')}")

        session = RealtimeSession(
            identity=identity,
            model=model,
            websocket=websocket,
            access_token=access_token,
        )
        try:
            await session.run()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"[realtime] Unhandled error: {e}")
        finally:
            await session.close()

    return router
