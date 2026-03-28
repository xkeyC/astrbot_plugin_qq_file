"""Utility functions for QQ File Plugin"""

import asyncio
import socket
from io import BytesIO
from typing import Optional
from ipaddress import ip_address
from astrbot.api import logger


def format_file_size(size_bytes: int) -> str:
    """Format file size to human readable string"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def get_lan_ip() -> Optional[str]:
    """Get LAN IP address (non-localhost)"""
    try:
        import psutil

        net_interfaces = psutil.net_if_addrs()
        for _, addrs in net_interfaces.items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = ip_address(addr.address)
                    if not ip.is_loopback and not ip.is_link_local:
                        return str(ip)
        return None
    except Exception as e:
        logger.warning(f"[QQFile] 获取局域网IP失败: {e}")
        return None


class TemporaryUploadServer:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        timeout: int,
        bot,
        group_id: int,
        filename: str,
        folder_id: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout
        self.bot = bot
        self.group_id = group_id
        self.filename = filename
        self.folder_id = folder_id
        self._server = None
        self._runner = None
        self._timeout_task = None
        self._file_received = asyncio.Event()

    async def start(self):
        from aiohttp import web

        app = web.Application()
        app.router.add_post("/upload", self._handle_upload)

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        loop = asyncio.get_event_loop()
        self._server = await loop.create_server(
            self._runner.server, self.host, self.port
        )
        self.port = self._server.sockets[0].getsockname()[1]

        self._timeout_task = asyncio.create_task(self._timeout_handler())

    async def _timeout_handler(self):
        try:
            await asyncio.wait_for(self._file_received.wait(), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.warning("[QQFile] 上传超时，关闭服务器")
        finally:
            await self.stop()

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._runner:
            await self._runner.cleanup()
        if self._timeout_task:
            self._timeout_task.cancel()

    async def _handle_upload(self, request):
        from aiohttp import web

        token = request.query.get("token")
        if token != self.token:
            return web.json_response(
                {"success": False, "error": "Invalid token"}, status=403
            )

        try:
            file_bytes = await request.read()
            if not file_bytes:
                return web.json_response(
                    {"success": False, "error": "Empty body"}, status=400
                )

            file_data = BytesIO(file_bytes)
            file_size = len(file_bytes)

            logger.info(
                f"[QQFile] 收到文件上传: {self.filename}, 大小: {format_file_size(file_size)}"
            )

            upload_result = await self._upload_to_qq(file_data)
            self._file_received.set()

            if upload_result.get("success"):
                asyncio.create_task(self._delayed_stop())
                return web.json_response(
                    {
                        "success": True,
                        "message": f"File '{self.filename}' uploaded successfully",
                        "file_id": upload_result.get("file_id"),
                    }
                )
            else:
                return web.json_response(
                    {
                        "success": False,
                        "error": upload_result.get("error", "Upload failed"),
                    },
                    status=500,
                )

        except Exception as e:
            logger.error(f"[QQFile] 处理上传失败: {e}")
            return web.json_response({"success": False, "error": str(e)}, status=500)

    async def _upload_to_qq(self, file_data: BytesIO) -> dict:
        import tempfile
        import os

        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=f"_{self.filename}"
            ) as tmp:
                tmp.write(file_data.getvalue())
                tmp_path = tmp.name

            try:
                result = await self.bot.api.call_action(
                    "upload_group_file",
                    group_id=self.group_id,
                    file=tmp_path,
                    name=self.filename,
                    folder=self.folder_id,
                )

                logger.info(
                    f"[QQFile] 文件上传成功: {self.filename} -> 群 {self.group_id}"
                )
                return {"success": True, "file_id": result.get("file_id")}

            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        except Exception as e:
            logger.error(f"[QQFile] 上传到QQ失败: {e}")
            return {"success": False, "error": str(e)}

    async def _delayed_stop(self):
        await asyncio.sleep(2)
        await self.stop()
