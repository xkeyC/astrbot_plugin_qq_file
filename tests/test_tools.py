import asyncio
import json
from pathlib import Path
import sys
import types
import unittest


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    astrbot.__path__ = []
    api = types.ModuleType("astrbot.api")
    api.__path__ = []
    star = types.ModuleType("astrbot.api.star")
    event = types.ModuleType("astrbot.api.event")
    components = types.ModuleType("astrbot.api.message_components")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class AstrMessageEvent:
        pass

    class File:
        pass

    class Logger:
        def __getattr__(self, _name):
            return lambda *_args, **_kwargs: None

    class EventMessageType:
        ALL = "all"

    class Filter:
        @staticmethod
        def event_message_type(_message_type):
            return lambda function: function

    Filter.EventMessageType = EventMessageType

    def register(*_args, **_kwargs):
        return lambda cls: cls

    def llm_tool(name):
        def decorator(function):
            function.__llm_tool_name__ = name
            return function

        return decorator

    api.llm_tool = llm_tool
    api.logger = Logger()
    star.Context = Context
    star.Star = Star
    star.register = register
    event.AstrMessageEvent = AstrMessageEvent
    event.filter = Filter
    components.File = File

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.star": star,
            "astrbot.api.event": event,
            "astrbot.api.message_components": components,
        }
    )


_install_astrbot_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_qq_file.config import PluginConfig  # noqa: E402
from astrbot_plugin_qq_file.main import QQFilePlugin  # noqa: E402


class FakeApi:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        response = self.responses[action]
        return response(kwargs) if callable(response) else response


class FakeBot:
    def __init__(self, responses):
        self.api = FakeApi(responses)


class FakeEvent:
    def __init__(self, bot, group_id=123456, sender_id=987654):
        self.bot = bot
        self._group_id = group_id
        self._sender_id = sender_id

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id


def make_plugin():
    plugin = object.__new__(QQFilePlugin)
    plugin.config = PluginConfig({"max_file_list_limit": 50})
    return plugin


class QQToolTests(unittest.TestCase):
    def test_all_llm_tools_use_qq_prefix(self):
        tool_names = [
            value.__llm_tool_name__
            for value in QQFilePlugin.__dict__.values()
            if hasattr(value, "__llm_tool_name__")
        ]
        self.assertTrue(tool_names)
        self.assertTrue(all(name.startswith("qq_") for name in tool_names))
        self.assertIn("qq_file", tool_names)
        self.assertIn("qq_group_members", tool_names)

    def test_group_members_are_role_sorted_and_paginated(self):
        bot = FakeBot(
            {
                "get_group_member_list": [
                    {"user_id": 3, "nickname": "普通成员", "role": "member"},
                    {"user_id": 2, "nickname": "管理员", "role": "admin"},
                    {"user_id": 1, "nickname": "群主", "role": "owner"},
                ]
            }
        )
        event = FakeEvent(bot)

        result = json.loads(
            asyncio.run(make_plugin().qq_group_members(event, page=1, page_size=2))
        )
        second_page = json.loads(
            asyncio.run(make_plugin().qq_group_members(event, page=2, page_size=2))
        )

        self.assertEqual(result["role_counts"], {"owner": 1, "admin": 1, "member": 1})
        self.assertEqual([item["role"] for item in result["members"]], ["owner", "admin"])
        self.assertEqual([item["role_name"] for item in result["members"]], ["群主", "管理员"])
        self.assertEqual(result["total_pages"], 2)
        self.assertTrue(result["has_next"])
        self.assertEqual([item["role"] for item in second_page["members"]], ["member"])
        self.assertFalse(second_page["has_next"])

    def test_group_file_items_include_source(self):
        bot = FakeBot(
            {
                "get_group_root_files": {
                    "files": [{"file_id": "f1", "file_name": "报告.pdf", "file_size": 1024}],
                    "folders": [{"folder_id": "d1", "folder_name": "资料"}],
                }
            }
        )

        result = json.loads(asyncio.run(make_plugin().qq_file(FakeEvent(bot))))

        self.assertEqual(result["source"], "group_file")
        self.assertEqual(result["files"][0]["source"], "group_file")
        self.assertEqual(result["folders"][0]["source"], "group_file")

    def test_album_local_pagination_does_not_skip_items(self):
        albums = [
            {"album_id": "a1", "name": "一"},
            {"album_id": "a2", "name": "二"},
            {"album_id": "a3", "name": "三"},
        ]
        bot = FakeBot(
            {
                "get_qun_album_list": {
                    "album_list": albums,
                    "attach_info": "",
                    "has_more": False,
                }
            }
        )
        plugin = make_plugin()
        event = FakeEvent(bot)

        first = json.loads(
            asyncio.run(plugin.qq_file(event, source="group_album", limit=2))
        )
        second = json.loads(
            asyncio.run(
                plugin.qq_file(
                    event,
                    source="group_album",
                    cursor=first["next_cursor"],
                    limit=2,
                )
            )
        )

        self.assertEqual([item["id"] for item in first["folders"]], ["a1", "a2"])
        self.assertTrue(first["has_more"])
        self.assertEqual([item["id"] for item in second["folders"]], ["a3"])
        self.assertFalse(second["has_more"])
        self.assertEqual(bot.api.calls[1][1]["attach_info"], "")

    def test_nested_album_media_items_are_expanded_and_paginated(self):
        bot = FakeBot(
            {
                "get_group_album_media_list": {
                    "media_list": [
                        {
                            "cell_common": {"time": "1750000000"},
                            "cell_user_info": {
                                "user": {"uin": "42", "nick": "上传者"}
                            },
                            "cell_media": {
                                "album_id": "a1",
                                "batch_id": "b1",
                                "media_items": [
                                    {
                                        "image": {
                                            "lloc": "m1",
                                            "name": "照片.jpg",
                                            "origin_url": "https://example.test/photo.jpg",
                                        }
                                    },
                                    {
                                        "video": {
                                            "lloc": "m2",
                                            "name": "视频.mp4",
                                            "url": "https://example.test/video.mp4",
                                        }
                                    },
                                ],
                            },
                        }
                    ],
                    "has_more": False,
                }
            }
        )

        first = json.loads(
            asyncio.run(
                make_plugin().qq_file(
                    FakeEvent(bot), source="group_album", album_id="a1", limit=1
                )
            )
        )
        second = json.loads(
            asyncio.run(
                make_plugin().qq_file(
                    FakeEvent(bot),
                    source="group_album",
                    album_id="a1",
                    cursor=first["next_cursor"],
                    limit=1,
                )
            )
        )

        image = first["files"][0]
        video = second["files"][0]
        self.assertEqual(image["source"], "group_album")
        self.assertEqual(image["file_type"], "album_media")
        self.assertEqual(image["media_type"], "image")
        self.assertEqual(image["id"], "m1")
        self.assertEqual(image["url"], "https://example.test/photo.jpg")
        self.assertEqual(image["album_id"], "a1")
        self.assertEqual(image["uploader_id"], "42")
        self.assertTrue(first["has_more"])
        self.assertEqual(video["media_type"], "video")
        self.assertEqual(video["id"], "m2")
        self.assertEqual(video["url"], "https://example.test/video.mp4")
        self.assertFalse(second["has_more"])


if __name__ == "__main__":
    unittest.main()
