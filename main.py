"""QQ File Plugin - Main Entry Point"""

import base64
import json
import math
import time
import uuid
from typing import Any, Optional
from astrbot.api.star import Context, Star, register
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from .config import PluginConfig
from .utils import format_file_size, get_lan_ip, TemporaryUploadServer


@register(
    "astrbot_plugin_qq_file",
    "xkeyC",
    "QQ信息与文件管理插件，支持LLM查询群文件、群相册、群成员并自动处理上传文件",
    "3.2.0",
)
class QQFilePlugin(Star):
    """QQ信息与文件管理插件 - 支持LLM Tools和文件上传监听"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = PluginConfig(config or {})
        self._setup_data_dir()
        self._processed_files: dict[str, float] = {}  # 用于去重

    def _setup_data_dir(self):
        """Initialize data directory"""
        import os

        self.data_dir = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(self.data_dir, exist_ok=True)

    async def terminate(self) -> None:
        """Clean up servers when plugin is unloaded"""
        await TemporaryUploadServer.stop_all()

    @staticmethod
    def _format_group_file(file_data: dict[str, Any]) -> dict[str, Any]:
        """Map a NapCat group-file item to the common qq_file shape."""
        size = file_data.get("file_size", file_data.get("size", 0)) or 0
        return {
            "type": "file",
            "source": "group_file",
            "id": file_data.get("file_id"),
            "name": file_data.get("file_name"),
            "size": format_file_size(int(size)),
            "size_bytes": int(size),
            "busid": file_data.get("busid"),
            "upload_time": file_data.get("upload_time"),
            "uploader_id": file_data.get("uploader"),
            "uploader": file_data.get("uploader_name"),
        }

    @staticmethod
    def _format_group_folder(folder_data: dict[str, Any]) -> dict[str, Any]:
        """Map a NapCat group-file folder to the common qq_file shape."""
        return {
            "type": "folder",
            "source": "group_file",
            "id": folder_data.get("folder_id"),
            "name": folder_data.get("folder_name"),
            "create_time": folder_data.get("create_time"),
            "creator_id": folder_data.get("creator"),
            "creator": folder_data.get("creator_name"),
            "file_count": folder_data.get("total_file_count"),
        }

    @staticmethod
    def _first_value(data: dict[str, Any], *keys: str) -> Any:
        """Read the first non-empty value, including common nested media objects."""
        containers = [data]
        for container_name in (
            "media_info",
            "photo_info",
            "pic_info",
            "file_info",
            "video_info",
        ):
            container = data.get(container_name)
            if isinstance(container, dict):
                containers.append(container)

        for key in keys:
            for container in containers:
                value = container.get(key)
                if value not in (None, "", []):
                    return value
        return None

    @classmethod
    def _format_group_album(cls, album: dict[str, Any]) -> dict[str, Any]:
        """Map a NapCat album to a folder-like qq_file item."""
        return {
            "type": "folder",
            "folder_type": "album",
            "source": "group_album",
            "id": cls._first_value(album, "album_id", "id"),
            "name": cls._first_value(album, "album_name", "name", "title"),
            "cover_url": cls._first_value(album, "cover_url", "cover"),
            "create_time": cls._first_value(album, "create_time", "createTime"),
            "update_time": cls._first_value(album, "update_time", "updateTime"),
            "creator_id": cls._first_value(album, "creator", "creator_id", "uin"),
            "media_count": cls._first_value(
                album, "media_count", "photo_count", "pic_count", "count"
            ),
        }

    @staticmethod
    def _expand_group_album_media_feeds(
        feeds: list[Any],
    ) -> list[dict[str, Any]]:
        """Expand NapCat cell_media.media_items so one qq_file means one media."""
        expanded: list[dict[str, Any]] = []
        for feed in feeds:
            if not isinstance(feed, dict):
                continue

            cell_media = feed.get("cell_media")
            media_items = (
                cell_media.get("media_items") if isinstance(cell_media, dict) else None
            )
            if not isinstance(media_items, list):
                # Keep compatibility with older/flattened NapCat response shapes.
                expanded.append(feed)
                continue

            cell_common = feed.get("cell_common")
            cell_common = cell_common if isinstance(cell_common, dict) else {}
            cell_user_info = feed.get("cell_user_info")
            cell_user_info = (
                cell_user_info if isinstance(cell_user_info, dict) else {}
            )
            user = cell_user_info.get("user")
            user = user if isinstance(user, dict) else {}

            for media_item in media_items:
                if not isinstance(media_item, dict):
                    continue

                normalized = {
                    "album_id": cell_media.get("album_id"),
                    "batch_id": cell_media.get("batch_id"),
                    "upload_time": cell_common.get("time"),
                    "uploader_id": user.get("uin", user.get("user_id")),
                    "uploader_name": user.get(
                        "nickname", user.get("nick", user.get("name"))
                    ),
                    "media_item": media_item,
                }
                for key, value in media_item.items():
                    if key not in ("image", "video"):
                        normalized[key] = value

                # A video item may also carry an image thumbnail, so prefer video.
                for media_type in ("video", "image"):
                    media_data = media_item.get(media_type)
                    if isinstance(media_data, dict):
                        normalized.update(media_data)
                        normalized["media_type"] = media_type
                        normalized[media_type] = media_data
                        break

                expanded.append(normalized)

        return expanded

    @classmethod
    def _format_group_album_media(cls, media: dict[str, Any]) -> dict[str, Any]:
        """Map a NapCat album media item to the common qq_file shape."""
        media_id = cls._first_value(media, "media_id", "lloc", "id", "batch_id")
        name = cls._first_value(
            media, "file_name", "name", "photo_name", "title", "desc"
        )
        size = cls._first_value(media, "file_size", "size")
        try:
            size_bytes = int(size or 0)
        except (TypeError, ValueError):
            size_bytes = 0

        return {
            "type": "file",
            "file_type": "album_media",
            "media_type": cls._first_value(media, "media_type", "type"),
            "source": "group_album",
            "id": media_id,
            "name": name or (f"相册媒体-{media_id}" if media_id else "相册媒体"),
            "size": format_file_size(size_bytes) if size is not None else None,
            "size_bytes": size_bytes if size is not None else None,
            "url": cls._first_value(
                media,
                "url",
                "origin_url",
                "original_url",
                "download_url",
                "raw_url",
            ),
            "thumbnail_url": cls._first_value(
                media, "thumbnail_url", "thumb_url", "cover_url"
            ),
            "upload_time": cls._first_value(
                media, "upload_time", "create_time", "shoot_time", "time"
            ),
            "uploader_id": cls._first_value(
                media, "uploader", "uploader_id", "user_id", "uin"
            ),
            "uploader": cls._first_value(
                media, "uploader_name", "nickname", "nick", "user_name"
            ),
            "album_id": cls._first_value(media, "album_id"),
            "batch_id": cls._first_value(media, "batch_id"),
            "lloc": cls._first_value(media, "lloc"),
        }

    @staticmethod
    def _unwrap_response(result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            return {}
        response = result.get("response")
        return response if isinstance(response, dict) else result

    @staticmethod
    def _encode_album_cursor(attach_info: str, offset: int) -> str:
        payload = json.dumps(
            {"attach_info": attach_info, "offset": offset},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return "qqac1_" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_album_cursor(cursor: Optional[str]) -> tuple[str, int]:
        if not cursor or not cursor.startswith("qqac1_"):
            return cursor or "", 0
        encoded = cursor.removeprefix("qqac1_")
        try:
            encoded += "=" * (-len(encoded) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
            return str(payload.get("attach_info", "")), max(int(payload.get("offset", 0)), 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            return cursor, 0

    @classmethod
    def _paginate_album_items(
        cls,
        items: list[Any],
        request_attach_info: str,
        offset: int,
        limit: int,
        response_attach_info: str,
        protocol_has_more: bool,
    ) -> tuple[list[dict[str, Any]], Optional[str], bool]:
        normalized_items = [item for item in items if isinstance(item, dict)]
        page_items = normalized_items[offset : offset + limit]
        next_offset = offset + len(page_items)

        if next_offset < len(normalized_items):
            next_cursor = cls._encode_album_cursor(request_attach_info, next_offset)
            return page_items, next_cursor, True
        if protocol_has_more and response_attach_info:
            return page_items, response_attach_info, True
        return page_items, None, False

    @llm_tool("qq_file")
    async def qq_file(
        self,
        event: AstrMessageEvent,
        group_id: Optional[int] = None,
        source: str = "group_file",
        folder_id: Optional[str] = None,
        album_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """统一查询QQ群文件或群相册。当用户询问群文件、群相册、照片或视频时调用。

        Args:
            group_id(number): 可选。群号。不提供时自动从当前会话获取。
            source(string): 可选。来源：group_file（群文件，默认）或 group_album（群相册）。
            folder_id(string): 可选。文件夹ID，用于列出特定文件夹内的文件。不提供则列出根目录文件。
            album_id(string): 可选。相册ID。source=group_album 时，不提供则列相册，提供则列相册媒体。
            cursor(string): 可选。群相册分页游标，使用上一次返回的 next_cursor。
            limit(number): 可选。返回的最大文件/文件夹数量。默认为20。

        Returns:
            str: 统一的 qq_file JSON；每个条目都有 source 标明来源
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            limit = max(1, min(limit, self.config.max_file_list_limit))
            source_aliases = {
                "file": "group_file",
                "files": "group_file",
                "group_file": "group_file",
                "album": "group_album",
                "albums": "group_album",
                "group_album": "group_album",
            }
            normalized_source = source_aliases.get(str(source or "").lower())
            if not normalized_source:
                return '{"error": "source 仅支持 group_file 或 group_album"}'

            if normalized_source == "group_file":
                if folder_id:
                    result = await bot.api.call_action(
                        "get_group_files_by_folder",
                        group_id=group_id,
                        folder_id=folder_id,
                    )
                else:
                    result = await bot.api.call_action(
                        "get_group_root_files",
                        group_id=group_id,
                    )

                result = self._unwrap_response(result)
                files = [
                    self._format_group_file(item)
                    for item in result.get("files", [])[:limit]
                    if isinstance(item, dict)
                ]
                folders = [
                    self._format_group_folder(item)
                    for item in result.get("folders", [])[:limit]
                    if isinstance(item, dict)
                ]

                return json.dumps(
                    {
                        "success": True,
                        "source": normalized_source,
                        "group_id": group_id,
                        "folder_id": folder_id or "root",
                        "file_count": len(files),
                        "folder_count": len(folders),
                        "files": files,
                        "folders": folders,
                    },
                    ensure_ascii=False,
                )

            request_attach_info, offset = self._decode_album_cursor(cursor)
            if album_id:
                result = await bot.api.call_action(
                    "get_group_album_media_list",
                    group_id=str(group_id),
                    album_id=album_id,
                    attach_info=request_attach_info,
                )
                result = self._unwrap_response(result)
                raw_items = result.get("media_list")
                if not isinstance(raw_items, list):
                    raw_items = result.get("feed_list", result.get("feeds", []))
                raw_items = self._expand_group_album_media_feeds(
                    raw_items if isinstance(raw_items, list) else []
                )
                view = "album_media"
            else:
                result = await bot.api.call_action(
                    "get_qun_album_list",
                    group_id=str(group_id),
                    attach_info=request_attach_info,
                )
                result = self._unwrap_response(result)
                raw_items = result.get("album_list", [])
                view = "albums"

            raw_items = raw_items if isinstance(raw_items, list) else []
            response_attach_info = str(result.get("attach_info") or "")
            protocol_has_more = bool(result.get("has_more", False))
            page_items, next_cursor, has_more = self._paginate_album_items(
                raw_items,
                request_attach_info,
                offset,
                limit,
                response_attach_info,
                protocol_has_more,
            )

            if album_id:
                files = [self._format_group_album_media(item) for item in page_items]
                folders = []
            else:
                files = []
                folders = [self._format_group_album(item) for item in page_items]

            return json.dumps(
                {
                    "success": True,
                    "source": normalized_source,
                    "view": view,
                    "group_id": group_id,
                    "album_id": album_id,
                    "file_count": len(files),
                    "folder_count": len(folders),
                    "files": files,
                    "folders": folders,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 列出文件失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_get_file_download_url")
    async def qq_get_file_download_url(
        self,
        event: AstrMessageEvent,
        file_id: str,
        group_id: Optional[int] = None,
    ) -> str:
        """获取QQ文件的下载链接。当用户需要下载某个文件时调用此工具。
        注意：获取到的下载链接不应暴露给用户，仅供内部处理使用。

        Args:
            file_id(string): 文件ID
            group_id(number): 可选。群号。不提供时自动从当前会话获取。

        Returns:
            str: 包含下载链接的JSON字符串
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            result = await bot.api.call_action(
                "get_group_file_url",
                group_id=group_id,
                file_id=file_id,
            )

            return json.dumps(
                {
                    "success": True,
                    "source": "group_file",
                    "url": result.get("url"),
                    "expire": result.get("expire"),
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 获取文件下载链接失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_search_files")
    async def qq_search_files(
        self,
        event: AstrMessageEvent,
        keyword: str,
        group_id: Optional[int] = None,
        folder_id: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """在QQ群文件中搜索匹配关键词的文件。当用户要查找特定文件时调用此工具。

        Args:
            keyword(string): 搜索关键词，匹配文件名和文件夹名
            group_id(number): 可选。群号。不提供时自动从当前会话获取。
            folder_id(string): 可选。文件夹ID，在指定文件夹内搜索。不提供则搜索根目录。
            limit(number): 可选。返回的最大结果数量。默认为20。

        Returns:
            str: 搜索结果的JSON字符串，包含匹配的文件和文件夹
        """
        if not self.config.enable_file_search:
            return '{"error": "文件搜索功能已禁用"}'

        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            limit = min(limit, self.config.max_file_list_limit)

            if folder_id:
                result = await bot.api.call_action(
                    "get_group_files_by_folder",
                    group_id=group_id,
                    folder_id=folder_id,
                )
            else:
                result = await bot.api.call_action(
                    "get_group_root_files",
                    group_id=group_id,
                )

            files = result.get("files", []) if isinstance(result, dict) else []
            folders = result.get("folders", []) if isinstance(result, dict) else []
            keyword_lower = keyword.lower()

            matched_files = []
            for f in files:
                file_name = f.get("file_name", "").lower()
                if keyword_lower in file_name:
                    matched_files.append(
                        {
                            "type": "file",
                            "source": "group_file",
                            "id": f.get("file_id"),
                            "name": f.get("file_name"),
                            "size": format_file_size(f.get("file_size", 0)),
                            "busid": f.get("busid"),
                            "upload_time": f.get("upload_time"),
                            "uploader": f.get("uploader_name"),
                        }
                    )
                    if len(matched_files) >= limit:
                        break

            matched_folders = []
            for fol in folders:
                folder_name = fol.get("folder_name", "").lower()
                if keyword_lower in folder_name:
                    matched_folders.append(
                        {
                            "type": "folder",
                            "source": "group_file",
                            "id": fol.get("folder_id"),
                            "name": fol.get("folder_name"),
                            "create_time": fol.get("create_time"),
                            "creator": fol.get("creator_name"),
                        }
                    )
                    if len(matched_folders) >= limit:
                        break

            return json.dumps(
                {
                    "success": True,
                    "source": "group_file",
                    "group_id": group_id,
                    "folder_id": folder_id or "root",
                    "keyword": keyword,
                    "file_count": len(matched_files),
                    "folder_count": len(matched_folders),
                    "files": matched_files,
                    "folders": matched_folders,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 搜索文件失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_get_file_info")
    async def qq_get_file_info(
        self,
        event: AstrMessageEvent,
        file_id: str,
        group_id: Optional[int] = None,
        folder_id: Optional[str] = None,
    ) -> str:
        """获取QQ文件的详细信息。当用户需要查看某个文件的详细资料时调用此工具。

        Args:
            file_id(string): 文件ID
            group_id(number): 可选。群号。不提供时自动从当前会话获取。
            folder_id(string): 可选。文件夹ID，文件所在文件夹。不提供则从根目录查找。

        Returns:
            str: 文件详细信息的JSON字符串
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            if folder_id:
                result = await bot.api.call_action(
                    "get_group_files_by_folder",
                    group_id=group_id,
                    folder_id=folder_id,
                )
            else:
                result = await bot.api.call_action(
                    "get_group_root_files",
                    group_id=group_id,
                )

            files = result.get("files", []) if isinstance(result, dict) else []

            file_data = None
            for f in files:
                if f.get("file_id") == file_id:
                    file_data = f
                    break

            if not file_data:
                return '{"error": "未找到该文件，请确认文件夹路径是否正确"}'

            return json.dumps(
                {
                    "success": True,
                    "source": "group_file",
                    "file": {
                        "source": "group_file",
                        "id": file_data.get("file_id"),
                        "name": file_data.get("file_name"),
                        "size": format_file_size(file_data.get("file_size", 0)),
                        "busid": file_data.get("busid"),
                        "upload_time": file_data.get("upload_time"),
                        "uploader_id": file_data.get("uploader"),
                        "uploader_name": file_data.get("uploader_name"),
                        "download_times": file_data.get("download_times", 0),
                    },
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 获取文件信息失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_request_file_upload")
    async def qq_request_file_upload(
        self,
        event: AstrMessageEvent,
        filename: str,
        group_id: Optional[int] = None,
        folder_id: Optional[str] = None,
        timeout: int = 300,
    ) -> str:
        """请求上传文件到QQ群。当文件已准备好时调用此工具。返回一个临时HTTP上传端点供客户端POST文件。

        重要：不要向用户展示upload_url和token等内部信息，只需告知用户"已准备好上传通道，请发送文件"。

        上传请求格式（使用Python requests）：
        ```python
        import requests
        with open(filename, 'rb') as f:
            requests.post(upload_url, data=f, headers={'Content-Type': 'application/octet-stream'})
        ```

        Args:
            filename(string): 文件名，如 "test.txt"
            group_id(number): 可选。目标群号。不提供时自动从当前会话获取。
            folder_id(string): 可选。目标文件夹ID。不提供则上传到根目录。
            timeout(number): 可选。上传等待超时时间（秒）。默认300秒。

        Returns:
            str: 包含上传URL和token的JSON字符串，客户端需在timeout秒内POST文件
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            lan_ip = get_lan_ip()
            if not lan_ip:
                return '{"error": "无法获取局域网IP地址"}'

            upload_token = str(uuid.uuid4())
            server = TemporaryUploadServer(
                host=lan_ip,
                port=0,
                token=upload_token,
                timeout=timeout,
                bot=bot,
                group_id=group_id,
                folder_id=folder_id,
                filename=filename,
            )

            await server.start()
            port = server.port
            upload_url = f"http://{lan_ip}:{port}/upload?token={upload_token}"

            logger.info(
                f"[QQFile] 文件上传服务器已启动: {upload_url}, 文件名: {filename}, 群: {group_id}, 超时: {timeout}秒"
            )

            return json.dumps(
                {
                    "success": True,
                    "upload_url": upload_url,
                    "token": upload_token,
                    "filename": filename,
                    "group_id": group_id,
                    "folder_id": folder_id or "root",
                    "timeout": timeout,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 启动上传服务器失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_delete_files")
    async def qq_delete_files(
        self,
        event: AstrMessageEvent,
        file_ids: str,
        group_id: Optional[int] = None,
    ) -> str:
        """批量删除QQ群文件。当用户需要删除群文件时调用此工具。

        Args:
            file_ids(string): 文件ID列表，多个ID用逗号分隔。如 "file_id1,file_id2,file_id3"
            group_id(number): 可选。群号。不提供时自动从当前会话获取。

        Returns:
            str: 删除结果的JSON字符串
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            ids = [fid.strip() for fid in file_ids.split(",") if fid.strip()]
            if not ids:
                return '{"error": "未提供有效的文件ID"}'

            results = []
            success_count = 0
            fail_count = 0

            for file_id in ids:
                try:
                    await bot.api.call_action(
                        "delete_group_file",
                        group_id=str(group_id),
                        file_id=file_id,
                    )
                    results.append({"file_id": file_id, "success": True})
                    success_count += 1
                    logger.info(f"[QQFile] 删除文件成功: {file_id} in 群 {group_id}")
                except Exception as e:
                    results.append(
                        {"file_id": file_id, "success": False, "error": str(e)}
                    )
                    fail_count += 1
                    logger.warning(f"[QQFile] 删除文件失败: {file_id}, 错误: {e}")

            return json.dumps(
                {
                    "success": True,
                    "group_id": group_id,
                    "total": len(ids),
                    "success_count": success_count,
                    "fail_count": fail_count,
                    "results": results,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 批量删除文件失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_delete_folder")
    async def qq_delete_folder(
        self,
        event: AstrMessageEvent,
        folder_id: str,
        group_id: Optional[int] = None,
    ) -> str:
        """删除QQ群文件夹。当用户需要删除群文件夹时调用此工具。注意：删除文件夹会删除其中的所有文件。

        Args:
            folder_id(string): 文件夹ID
            group_id(number): 可选。群号。不提供时自动从当前会话获取。

        Returns:
            str: 删除结果的JSON字符串
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            await bot.api.call_action(
                "delete_group_folder",
                group_id=str(group_id),
                folder_id=folder_id,
            )

            logger.info(f"[QQFile] 删除文件夹成功: {folder_id} in 群 {group_id}")
            return json.dumps(
                {
                    "success": True,
                    "group_id": group_id,
                    "folder_id": folder_id,
                    "message": "文件夹删除成功",
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 删除文件夹失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("qq_group_members")
    async def qq_group_members(
        self,
        event: AstrMessageEvent,
        group_id: Optional[int] = None,
        page: int = 1,
        page_size: int = 20,
        role: Optional[str] = None,
        no_cache: bool = False,
    ) -> str:
        """分页查询QQ群成员，明确返回群主、管理员和普通成员身份。

        Args:
            group_id(number): 可选。群号。不提供时自动从当前群聊获取。
            page(number): 可选。页码，从1开始。默认为1。
            page_size(number): 可选。每页人数，范围1-100。默认为20。
            role(string): 可选。按身份筛选：owner、admin 或 member。
            no_cache(boolean): 可选。是否要求 NapCat 刷新成员缓存。默认否。

        Returns:
            str: 成员分页JSON，包含身份统计、分页信息和成员列表
        """
        if group_id is None:
            group_id = event.get_group_id()
            if not group_id:
                return '{"error": "无法获取群号，请在群聊中使用或手动指定群号"}'

        if not self.config.check_access(group_id, event.get_sender_id()):
            return '{"error": "无权限访问"}'

        if page < 1:
            return '{"error": "page 必须从 1 开始"}'

        normalized_role = str(role).lower() if role else None
        if normalized_role not in (None, "owner", "admin", "member"):
            return '{"error": "role 仅支持 owner、admin 或 member"}'

        bot = getattr(event, "bot", None)
        if not bot:
            return '{"error": "Bot不可用"}'

        try:
            page_size = max(1, min(page_size, 100))
            result = await bot.api.call_action(
                "get_group_member_list",
                group_id=str(group_id),
                no_cache=no_cache,
            )

            if isinstance(result, list):
                raw_members = result
            else:
                result_data = self._unwrap_response(result)
                raw_members = result_data.get(
                    "members",
                    result_data.get("member_list", result_data.get("data", [])),
                )
            raw_members = raw_members if isinstance(raw_members, list) else []
            members = [item for item in raw_members if isinstance(item, dict)]

            role_counts = {"owner": 0, "admin": 0, "member": 0}
            for member in members:
                member_role = str(member.get("role") or "member").lower()
                if member_role not in role_counts:
                    member_role = "member"
                role_counts[member_role] += 1

            if normalized_role:
                members = [
                    member
                    for member in members
                    if str(member.get("role") or "member").lower()
                    == normalized_role
                ]

            role_order = {"owner": 0, "admin": 1, "member": 2}
            members.sort(
                key=lambda member: (
                    role_order.get(str(member.get("role") or "member").lower(), 2),
                    str(member.get("card") or member.get("nickname") or ""),
                    str(member.get("user_id") or ""),
                )
            )

            total = len(members)
            total_pages = math.ceil(total / page_size) if total else 0
            start = (page - 1) * page_size
            page_members = members[start : start + page_size]
            role_names = {
                "owner": "群主",
                "admin": "管理员",
                "member": "成员",
            }
            formatted_members = []
            for member in page_members:
                member_role = str(member.get("role") or "member").lower()
                if member_role not in role_names:
                    member_role = "member"
                formatted_members.append(
                    {
                        "user_id": member.get("user_id"),
                        "nickname": member.get("nickname"),
                        "card": member.get("card"),
                        "display_name": member.get("card") or member.get("nickname"),
                        "role": member_role,
                        "role_name": role_names[member_role],
                        "title": member.get("title"),
                        "join_time": member.get("join_time"),
                        "last_sent_time": member.get("last_sent_time"),
                        "level": member.get("level"),
                        "is_robot": member.get("is_robot", False),
                    }
                )

            return json.dumps(
                {
                    "success": True,
                    "group_id": group_id,
                    "role_filter": normalized_role,
                    "role_counts": role_counts,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": total_pages,
                    "has_previous": page > 1,
                    "has_next": page < total_pages,
                    "members": formatted_members,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.error(f"[QQFile] 查询群成员失败: {e}")
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_group_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件（支持通知事件和File组件消息）"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        raw_msg = event.message_obj.raw_message

        if isinstance(raw_msg, dict) and raw_msg.get("notice_type") == "group_upload":
            file_info = raw_msg.get("file", {})
            file_name = file_info.get("name", "")
            file_id = file_info.get("id")
            file_size = file_info.get("size", 0)
            _busid = file_info.get("busid")
        else:
            file_components = [
                comp for comp in event.message_obj.message if isinstance(comp, File)
            ]
            if not file_components:
                return

            file_comp = file_components[0]
            file_name = file_comp.name or ""
            file_id = getattr(file_comp, "file_id", None)
            file_size = 0
            _busid = None

            if isinstance(raw_msg, dict):
                msg_data = raw_msg.get("message", [])
                if msg_data and isinstance(msg_data, list):
                    for seg in msg_data:
                        if isinstance(seg, dict) and seg.get("type") == "file":
                            data = seg.get("data", {})
                            file_id = data.get("file_id", file_id)
                            file_size = data.get("file_size", 0) or data.get("size", 0)
                            _busid = data.get("busid")
                            break

        if not file_name:
            return

        if not group_id:
            return

        # 确保 group_id 为整数类型
        try:
            group_id = int(group_id)
        except (ValueError, TypeError):
            logger.warning(f"[QQFile] 无效的 group_id: {group_id}")
            return

        # 去重：同一群同一文件在5秒内只处理一次
        dedup_key = f"{group_id}:{file_name}"
        now = time.time()
        if dedup_key in self._processed_files:
            if now - self._processed_files[dedup_key] < 5:
                logger.debug(f"[QQFile] 跳过重复文件事件: {file_name}")
                return
        self._processed_files[dedup_key] = now

        # 清理过期的去重记录
        expired_keys = [k for k, v in self._processed_files.items() if now - v > 60]
        for k in expired_keys:
            del self._processed_files[k]

        logger.info(
            f"[QQFile] 检测到群文件上传: {file_name} 在群 {group_id} 由用户 {user_id}"
        )

        if not self.config.enable_auto_process:
            logger.info(
                "[QQFile] 文件自动处理功能未启用，请在配置中开启 enable_auto_process"
            )
            return

        template = self.config.match_auto_process_template(group_id, file_name)

        if not template:
            logger.info(
                f"[QQFile] 未匹配到自动处理模板: file_name={file_name}, group_id={group_id}"
            )
            return

        logger.info(f"[QQFile] 触发自动处理: {file_name} (匹配模板)")

        file_url = None
        bot = getattr(event, "bot", None)
        if bot and file_id:
            try:
                result = await bot.api.call_action(
                    "get_group_file_url",
                    group_id=group_id,
                    file_id=file_id,
                )
                if result and isinstance(result, dict):
                    file_url = result.get("url")
            except Exception as e:
                logger.warning(f"[QQFile] 获取文件下载链接失败: {e}")

        file_context = self._build_file_context(
            file_name=file_name,
            file_url=file_url,
            file_size=file_size,
            group_id=group_id,
            user_id=user_id,
        )

        custom_prompt = template.prompt.strip() if template.prompt else None

        event.is_at_or_wake_command = True
        event.is_wake = True
        yield event.request_llm(
            prompt=file_context,
            system_prompt=custom_prompt,
        )

    def _build_file_context(
        self,
        file_name: str,
        file_url: str | None,
        file_size: int,
        group_id: int,
        user_id: int,
    ) -> str:
        """构建文件上下文消息。注意：下载链接仅供内部处理，不应暴露给用户。"""
        lines = [
            "[系统提示] 检测到群文件上传",
            f"上传者: {user_id}",
            f"文件名: {file_name}",
            f"文件大小: {format_file_size(file_size)}",
        ]

        if file_url:
            lines.append(f"文件下载链接: {file_url}")
            lines.append("[注意] 下载链接仅供内部处理使用，请勿在回复中暴露给用户")

        lines.append("请根据上传的文件内容进行处理。")
        return "\n".join(lines)
