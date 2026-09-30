from starlette.datastructures import Headers
from starlette.middleware.gzip import GZipMiddleware


class StaticAssetCompression(GZipMiddleware):
    """Compress build assets without ever buffering API streams or byte ranges."""
    async def __call__(self, scope, receive, send):
        if (scope['type'] == 'http' and scope['path'].startswith('/_next/')
                and scope['method'] == 'GET' and 'range' not in Headers(scope=scope)):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)
