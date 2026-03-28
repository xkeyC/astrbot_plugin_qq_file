"""Configuration management for QQ File Plugin"""

from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import re


@dataclass
class AutoProcessTemplate:
    """Auto-processing template configuration"""

    group_ids: List[int]
    file_patterns: List[re.Pattern]
    prompt: str  # 自定义提示词，留空使用默认


class PluginConfig:
    """Plugin configuration handler"""

    def __init__(self, config: dict):
        self._raw = config

        # Access control
        self.access_mode = config.get("access_mode", "blacklist")
        self.group_list = self._parse_id_list(config.get("group_list", ""))

        # File listing
        self.max_file_list_limit = config.get("max_file_list_limit", 50)
        self.enable_file_search = config.get("enable_file_search", True)

        # Auto processing templates
        self.enable_auto_process = config.get("enable_auto_process", False)
        self.auto_process_templates = self._parse_templates(
            config.get("auto_process_templates", [])
        )

    def _parse_id_list(self, value) -> List[int]:
        """Parse comma or newline-separated ID string to list of integers"""
        if isinstance(value, str):
            # Support both comma and newline separators
            ids = []
            for line in value.replace(",", "\n").split("\n"):
                line = line.strip()
                if line and line.isdigit():
                    ids.append(int(line))
            return ids
        elif isinstance(value, list):
            return [
                int(x) for x in value if isinstance(x, (int, str)) and str(x).isdigit()
            ]
        return []

    def _parse_patterns_from_textarea(self, value: str) -> List[re.Pattern]:
        """Parse newline-separated regex patterns from textarea"""
        patterns = []
        if not value or not isinstance(value, str):
            return patterns

        for line in value.split("\n"):
            pattern_str = line.strip()
            if pattern_str:
                try:
                    patterns.append(re.compile(pattern_str))
                except re.error as e:
                    from astrbot.api import logger

                    logger.warning(
                        f"[QQFile] Invalid regex pattern '{pattern_str}': {e}"
                    )
        return patterns

    def _parse_templates(
        self, templates_data: List[Dict[str, Any]]
    ) -> List[AutoProcessTemplate]:
        """Parse auto-processing templates from config"""
        templates = []

        if not isinstance(templates_data, list):
            return templates

        for template_data in templates_data:
            if not isinstance(template_data, dict):
                continue

            # Parse group IDs from textarea (one per line)
            group_ids_text = template_data.get("group_ids", "")
            group_ids = self._parse_id_list(group_ids_text)

            # Parse file patterns from textarea (one per line)
            patterns_text = template_data.get("file_patterns", "")
            file_patterns = self._parse_patterns_from_textarea(patterns_text)

            # Parse prompt (multi-line text)
            prompt = template_data.get("prompt", "") or ""

            templates.append(
                AutoProcessTemplate(
                    group_ids=group_ids,
                    file_patterns=file_patterns,
                    prompt=prompt,
                )
            )

        return templates

    def check_access(
        self, group_id: Optional[int] = None, user_id: Optional[int] = None
    ) -> bool:
        """Check if group or user has access permission"""
        if self.access_mode == "whitelist":
            if group_id:
                return group_id in self.group_list
            return False
        else:  # blacklist
            if group_id:
                return group_id not in self.group_list
            return user_id not in self.group_list if user_id else True

    def match_auto_process_template(
        self, group_id: int, file_name: str
    ) -> Optional[AutoProcessTemplate]:
        """
        Match file against auto-processing templates from top to bottom.
        Returns the first matching template or None.
        """
        if not self.enable_auto_process:
            return None

        from astrbot.api import logger

        for i, template in enumerate(self.auto_process_templates):
            # Check group match (empty group_ids means match all groups)
            if template.group_ids:
                if group_id not in template.group_ids:
                    logger.debug(
                        f"[QQFile] 模板[{i}] 群号不匹配: {group_id} not in {template.group_ids}"
                    )
                    continue

            # Check file pattern match (empty patterns means match all files)
            if template.file_patterns:
                matched = any(
                    pattern.search(file_name) for pattern in template.file_patterns
                )
                if not matched:
                    logger.debug(f"[QQFile] 模板[{i}] 文件名不匹配: {file_name}")
                    continue

            # This template matches
            return template

        return None
