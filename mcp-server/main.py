# -*- coding: utf-8 -*-
import os
import re
import socket
import sys
import httpx
import anyio
from mcp.server.fastmcp import FastMCP, Image, Context
import base64
from typing import Optional, Dict, Any, Union

# Create a generic MCP server for interacting with Revit
# Use stateless_http=True and json_response=True for better compatibility
mcp = FastMCP(
    "Revit MCP Server", 
    host="127.0.0.1", 
    port=8000,
    stateless_http=True,
    json_response=True
)

# Configuration
REVIT_HOST = os.environ.get("REVIT_HOST", "localhost")


def _discover_revit_port() -> int:
    """Find the port pyRevit Routes is actually listening on.

    Routes defaults to 48884, but if that port is still held when Revit starts
    (typically a previous Revit process that has not fully released it) it
    silently increments. Each live Routes server drops a serverinfo pickle in
    the pyRevit appdata folder recording its port, so use those as candidates
    and pick the first one that actually accepts a connection — a stale file
    from a dead process fails the connect and is skipped.
    """
    env_port = os.environ.get("REVIT_PORT")
    if env_port:
        return int(env_port)

    candidates = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        info_dir = os.path.join(appdata, "pyRevit")
        for root, _dirs, files in os.walk(info_dir):
            for name in files:
                if not name.endswith("_serverinfo.pickle"):
                    continue
                path = os.path.join(root, name)
                try:
                    with open(path, "rb") as handle:
                        blob = handle.read()
                except OSError:
                    continue
                match = re.search(rb"server_port'\s*\.?\s*p?\d*\s*\.?\s*I(\d+)", blob)
                if match:
                    candidates.append((os.path.getmtime(path), int(match.group(1))))

    # Newest serverinfo first, then the documented default as a last resort.
    ports = [port for _mtime, port in sorted(candidates, reverse=True)]
    ports.append(48884)

    seen = set()
    for port in ports:
        if port in seen:
            continue
        seen.add(port)
        try:
            # create_connection, not connect_ex: on Windows a socket with a
            # timeout set is non-blocking, so connect_ex returns WSAEWOULDBLOCK
            # rather than 0 and every port would look closed.
            with socket.create_connection((REVIT_HOST, port), timeout=1.0):
                return port
        except OSError:
            continue

    return 48884


REVIT_PORT = _discover_revit_port()
BASE_URL = f"http://{REVIT_HOST}:{REVIT_PORT}/revit_mcp"

# Shared HTTP client with keep-alive connection pooling. Reusing a single
# AsyncClient across all tool calls avoids the per-request TCP/handshake cost
# of creating a new client each time — meaningful when a session fires dozens
# of calls at the local Routes server.
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url=BASE_URL,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )
    return _http_client


async def revit_get(endpoint: str, ctx: Context = None, **kwargs) -> Union[Dict, str]:
    """Simple GET request to Revit API"""
    return await _revit_call("GET", endpoint, ctx=ctx, **kwargs)


async def revit_post(endpoint: str, data: Dict[str, Any], ctx: Context = None, **kwargs) -> Union[Dict, str]:
    """Simple POST request to Revit API"""
    return await _revit_call("POST", endpoint, data=data, ctx=ctx, **kwargs)


async def revit_image(endpoint: str, ctx: Context = None) -> Union[Image, str]:
    """GET request that returns an Image object"""
    try:
        client = _get_client()
        response = await client.get(endpoint, timeout=60.0)

        if response.status_code == 200:
            data = response.json()
            image_bytes = base64.b64decode(data["image_data"])
            return Image(data=image_bytes, format="png")
        else:
            return f"Error: {response.status_code} - {response.text}"
    except Exception as e:
        return f"Error: {e}"


async def _revit_call(method: str, endpoint: str, data: Dict = None, ctx: Context = None, 
                     timeout: float = 30.0, params: Dict = None) -> Union[Dict, str]:
    """Internal function handling all HTTP calls"""
    try:
        client = _get_client()

        if method == "GET":
            response = await client.get(endpoint, params=params, timeout=timeout)
        else:  # POST
            response = await client.post(
                endpoint,
                json=data,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )

        if response.status_code == 200:
            return response.json()

        # Non-200: the handler's structured error payload (error / traceback /
        # tx_status / hints) is valuable and format_response can render it
        # properly - but only when the body actually parses as a JSON object.
        # A body that doesn't (a proxy's HTML error page, a truncated read)
        # falls back to the original plain-text error, unchanged. _http_status
        # is stamped on so format_response can never mistake this dict for a
        # success, even if a "status": "success"-shaped body were nested
        # inside it.
        try:
            parsed = response.json()
        except Exception:
            return f"Error: {response.status_code} - {response.text}"
        if isinstance(parsed, dict):
            parsed["_http_status"] = response.status_code
            return parsed
        return f"Error: {response.status_code} - {response.text}"
    except Exception as e:
        return f"Error: {e}"


# Register all tools BEFORE the main block
from tools import register_tools
register_tools(mcp, revit_get, revit_post, revit_image)


async def run_combined_async():
    """Run server with both SSE and streamable-http endpoints.

    This allows clients to connect via either:
    - SSE: GET /sse, POST /messages/
    - Streamable-HTTP: POST/GET /mcp
    """
    import uvicorn

    # Get the streamable-http app first - it has the proper lifespan
    # that initializes the session manager's task group
    http_app = mcp.streamable_http_app()

    # Get SSE routes (SSE doesn't need special lifespan - it creates
    # task groups per-request in connect_sse())
    sse_app = mcp.sse_app()

    # Add SSE routes to the http app (preserving its lifespan)
    for route in sse_app.routes:
        http_app.routes.append(route)

    config = uvicorn.Config(
        http_app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    transport = "stdio"

    if "--sse" in sys.argv:
        transport = "sse"
    elif "--http" in sys.argv or "--streamable-http" in sys.argv:
        transport = "streamable-http"
    elif "--combined" in sys.argv:
        # Run both SSE and streamable-http transports simultaneously
        print("Starting combined server with SSE (/sse, /messages/) and streamable-http (/mcp) endpoints...")
        anyio.run(run_combined_async)
        sys.exit(0)

    mcp.run(transport=transport)