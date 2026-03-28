"""QQ File Plugin - Main Entry Point"""

import json
from typing import Optional
from astrbot.api.star import Context, Star, register
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from .config import PluginConfig, AutoProcessTemplate
from .utils import format_file_size


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

    @filter.event_message_type(filter.EventMessageType.OTHER_MESSAGE)
    async def on_group_upload(self, event: AstrMessageEvent):
        """处理群文件上传事件"""
        raw_msg = event.message_obj.raw_message

        if not isinstance(raw_msg, dict):
            return

        if raw_msg.get("notice_type") != "group_upload":
            return

        group_id = raw_msg.get("group_id")
        user_id = raw_msg.get("user_id")
        file_info = raw_msg.get("file", {})

        file_name = file_info.get("name", "")
        file_id = file_info.get("id")
        file_size = file_info.get("size", 0)
        busid = file_info.get("busid")

        logger.info(
            f"[QQFile] 检测到群文件上传: {file_name} 在群 {group_id} 由用户 {user_id}"
        )

        template = self.config.match_auto_process_template(group_id, file_name)

        if not template:
            logger.debug(f"[QQFile] 跳过自动处理: {file_name}")
            return

        logger.info(
            f"[QQFile] 触发自动处理: {file_name} "
            f"(匹配模板，建议技能: {template.skills})"
        )

        file_context = self._build_file_context(
            file_name=file_name,
            file_id=file_id,
            busid=busid,
            file_size=file_size,
            group_id=group_id,
            user_id=user_id,
            template=template,
        )

        try:
            await event.send(file_context)
        except Exception as e:
            logger.error(f"[QQFile] 发送文件上下文失败: {e}")

    def _build_file_context(
        self,
        file_name: str,
        file_id: str,
        busid: int,
        file_size: int,
        group_id: int,
        user_id: int,
        template: AutoProcessTemplate,
    ) -> str:
        """构建文件上下文消息"""
        lines = [
            "[系统提示] 检测到群文件上传",
            f"上传者: {user_id}",
            f"文件名: {file_name}",
            f"文件ID: {file_id}",
            f"文件大小: {format_file_size(file_size)}",
        ]

        if template.skills:
            skills_str = ", ".join(template.skills)
            lines.append(f"建议使用以下技能处理: {skills_str}")

        lines.append("请根据上传的文件内容进行处理。")
        return "\n".join(lines)
