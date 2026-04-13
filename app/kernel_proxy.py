"""WebSocket reverse proxy: browser <-> FastAPI <-> session pod kernel."""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from fastapi import WebSocket, WebSocketDisconnect

from .session_manager import Session, SessionManager

log = logging.getLogger(__name__)


async def proxy_kernel_websocket(
    websocket: WebSocket,
    session: Session,
    session_manager: SessionManager,
) -> None:
    kernel_ws_url = (
        f"ws://{session.pod_ip}:8888"
        f"/api/kernels/{session.kernel_id}/channels"
    )

    await websocket.accept()

    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                kernel_ws_url,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_connect=15),
            ) as kernel_ws:
                log.info(
                    "Kernel proxy open  session=%s kernel=%s",
                    session.session_id[:8],
                    session.kernel_id[:8],
                )

                async def browser_to_kernel() -> None:
                    try:
                        while True:
                            msg = await websocket.receive()
                            if msg["type"] == "websocket.disconnect":
                                break
                            text = msg.get("text")
                            if text is not None:
                                session_manager.touch_session(session.session_id)
                                await kernel_ws.send_str(text)
                            elif msg.get("bytes") is not None:
                                await kernel_ws.send_bytes(msg["bytes"])
                    except WebSocketDisconnect:
                        pass
                    except Exception as e:
                        log.debug("browser→kernel error: %s", e)
                    finally:
                        await kernel_ws.close()

                async def kernel_to_browser() -> None:
                    try:
                        async for msg in kernel_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                session_manager.touch_session(session.session_id)
                                await websocket.send_text(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await websocket.send_bytes(msg.data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                    except Exception as e:
                        log.debug("kernel→browser error: %s", e)
                    finally:
                        try:
                            await websocket.close()
                        except Exception:
                            pass

                await asyncio.gather(
                    browser_to_kernel(),
                    kernel_to_browser(),
                    return_exceptions=True,
                )

    except aiohttp.ClientConnectorError as e:
        log.error("Cannot connect to kernel WebSocket at %s: %s", kernel_ws_url, e)
        try:
            await websocket.close(code=1011, reason="Kernel unreachable")
        except Exception:
            pass
    except Exception as e:
        log.error("Kernel proxy error: %s", e)
        try:
            await websocket.close(code=1011, reason="Proxy error")
        except Exception:
            pass
    finally:
        log.info("Kernel proxy closed  session=%s", session.session_id[:8])
