"""QQ File Plugin - Main Entry Point"""

import json
import time
import uuid
from typing import Optional
from astrbot.api.star import Context, Star, register
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from .config import PluginConfig
from .utils import format_file_size, get_lan_ip, TemporaryUploadServer


@register(
    "astrbot_plugin_qq_file",
    "xkeyC",
    "QQ文件管理插件，支持LLM工具查询文件和自动处理上传文件",
    "3.0.0",
)
class QQFilePlugin(Star):
    """QQ文件管理插件 - 支持LLM Tools和文件上传监听"""

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

    @llm_tool("list_qq_files")
    async def list_qq_files(
        self,
        event: AstrMessageEvent,
        group_id: Optional[int] = None,
        folder_id: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """列出QQ群文件。当用户询问群文件、查看文件列表时调用此工具。如未指定群号，自动从当前会话获取。

        Args:
            group_id(number): 可选。群号。不提供时自动从当前会话获取。
            folder_id(string): 可选。文件夹ID，用于列出特定文件夹内的文件。不提供则列出根目录文件。
            limit(number): 可选。返回的最大文件/文件夹数量。默认为20。

        Returns:
            str: 文件列表信息的JSON字符串，包含文件和文件夹信息
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

            formatted_files = []
            for f in files[:limit]:
                formatted_files.append(
                    {
                        "type": "file",
                        "id": f.get("file_id"),
                        "name": f.get("file_name"),
                        "size": format_file_size(f.get("file_size", 0)),
                        "busid": f.get("busid"),
                        "upload_time": f.get("upload_time"),
                        "uploader": f.get("uploader_name"),
                    }
                )

            formatted_folders = []
            for fol in folders[:limit]:
                formatted_folders.append(
                    {
                        "type": "folder",
                        "id": fol.get("folder_id"),
                        "name": fol.get("folder_name"),
                        "create_time": fol.get("create_time"),
                        "creator": fol.get("creator_name"),
                    }
                )

            return json.dumps(
                {
                    "success": True,
                    "group_id": group_id,
                    "folder_id": folder_id or "root",
                    "file_count": len(formatted_files),
                    "folder_count": len(formatted_folders),
                    "files": formatted_files,
                    "folders": formatted_folders,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 列出文件失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("get_file_download_url")
    async def get_file_download_url(
        self,
        event: AstrMessageEvent,
        file_id: str,
        group_id: Optional[int] = None,
    ) -> str:
        """获取QQ文件的下载链接。当用户需要下载某个文件时调用此工具。

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
                    "url": result.get("url"),
                    "expire": result.get("expire"),
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 获取文件下载链接失败: {e}")
            return f'{{"error": "{str(e)}"}}'

    @llm_tool("search_qq_files")
    async def search_qq_files(
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

    @llm_tool("get_file_info")
    async def get_file_info(
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
                    "file": {
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

    @llm_tool("request_file_upload")
    async def request_file_upload(
        self,
        event: AstrMessageEvent,
        filename: str,
        group_id: Optional[int] = None,
        folder_id: Optional[str] = None,
        timeout: int = 300,
    ) -> str:
        """请求上传文件到QQ群。当用户需要上传文件到群文件时调用此工具。返回一个临时HTTP上传端点，客户端需要POST二进制文件数据到此端点。

        上传请求格式：
        - Method: POST
        - Content-Type: application/octet-stream
        - Body: 文件二进制数据
        - Query: token={返回的token}

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
                    "request_format": {
                        "method": "POST",
                        "content_type": "application/octet-stream",
                        "body": "binary file data",
                    },
                },
                ensure_ascii=False,
            )

        except Exception as e:
            logger.error(f"[QQFile] 启动上传服务器失败: {e}")
            return f'{{"error": "{str(e)}"}}'

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
        """构建文件上下文消息"""
        lines = [
            "[系统提示] 检测到群文件上传",
            f"上传者: {user_id}",
            f"文件名: {file_name}",
            f"文件大小: {format_file_size(file_size)}",
        ]

        if file_url:
            lines.append(f"文件下载链接: {file_url}")

        lines.append("请根据上传的文件内容进行处理。")
        return "\n".join(lines)
