from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field

from api.support import require_identity
from services.account_service import account_service
from services.realtime.session import RealtimeSession
from services.realtime.chatgpt_webrtc import exchange_realtime_sdp
from utils.log import logger


class RealtimeOffer(BaseModel):
    sdp: str = Field(min_length=100, max_length=100_000)
    voice: str = Field(default="ember", min_length=1, max_length=32)
    language: str = Field(default="auto", min_length=1, max_length=32)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.post("/v1/realtime/sessions")
    async def create_realtime_session(
        offer: RealtimeOffer,
        authorization: str | None = Header(default=None),
    ):
        """仅代理 SDP 信令；音频媒体在浏览器和 ChatGPT 之间直接走 WebRTC。"""
        identity = require_identity(authorization)
        try:
            access_token = account_service.get_realtime_access_token()
            answer_sdp, location = await exchange_realtime_sdp(
                access_token=access_token,
                offer_sdp=offer.sdp,
                voice=offer.voice,
                language=offer.language,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        logger.info(
            f"[realtime] Direct WebRTC session: voice={offer.voice}, "
            f"identity={identity.get('name')}"
        )
        return {"sdp": answer_sdp, "location": location}

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
            access_token_provider=account_service.get_realtime_access_token,
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
