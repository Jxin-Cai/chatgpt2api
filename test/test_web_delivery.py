import asyncio

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.testclient import TestClient
from uvicorn import Config
from uvicorn.server import ServerState
from websockets.frames import Opcode
from websockets.protocol import State

from api.static_compression import StaticAssetCompression
from services.realtime.server_protocol import RealtimeWebSocketProtocol


def test_pcm_backpressure_and_keepalive_can_wait_together():
    async def app(scope, receive, send):
        pass

    async def run():
        protocol = RealtimeWebSocketProtocol(Config(app, lifespan="off"), ServerState(), {})
        writes = []
        class Transport:
            def write(self, data): writes.append(data)
            def is_closing(self): return False
        protocol.transport = Transport()
        protocol.state = State.OPEN
        protocol.pause_writing()
        audio = asyncio.create_task(protocol.write_frame(True, Opcode.TEXT, b'pcm'))
        await asyncio.sleep(0)
        # This is the exact write_frame path used by keepalive_ping. The old
        # protocol asserts when both sends drain a congested TCP connection.
        ping = asyncio.create_task(protocol.write_frame(True, Opcode.PING, b'ping'))
        await asyncio.sleep(0)
        assert not audio.done() and not ping.done()
        protocol.resume_writing()
        await asyncio.wait_for(asyncio.gather(audio, ping), 1)
        assert len(writes) == 2
        assert protocol.state == State.OPEN
    asyncio.run(run())


def test_compress_assets_without_compressing_ranges_or_sse(tmp_path):
    content = b'const message = "stream remains live";\n' * 5000
    asset = tmp_path / 'app.js'
    asset.write_bytes(content)
    app = FastAPI()
    app.add_middleware(StaticAssetCompression, minimum_size=1024, compresslevel=5)
    @app.get('/_next/static/app.js')
    def script(): return FileResponse(asset)
    @app.get('/v1/events')
    def events(): return StreamingResponse(iter([b'data: first\n\n', b'data: ' + b'x' * 2000 + b'\n\n']), media_type='text/event-stream')
    with TestClient(app) as client:
        response = client.get('/_next/static/app.js', headers={'Accept-Encoding': 'gzip'})
        assert response.headers['content-encoding'] == 'gzip'
        assert response.content == content
        partial = client.get('/_next/static/app.js', headers={'Accept-Encoding': 'gzip', 'Range': 'bytes=10-29'})
        assert partial.status_code == 206
        assert 'content-encoding' not in partial.headers
        assert partial.content == content[10:30]
        stream = client.get('/v1/events', headers={'Accept-Encoding': 'gzip'})
        assert 'content-encoding' not in stream.headers
        assert stream.content.startswith(b'data: first\n\n')
