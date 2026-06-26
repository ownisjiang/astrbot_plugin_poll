"""
AstrBot 投票插件 PollStar
===========================
在聊天中创建投票、参与投票、查看结果。

功能:
  - 创建单选/多选投票
  - 公开/匿名投票
  - 定时截止
  - 实时查看结果
  - 数据持久化

命令:
  /poll create "问题" "选项1" "选项2" ...
  /poll vote <id> <选项号>
  /poll result <id>
  /poll list
  /poll close <id>
  /poll help
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Plain


# ── 持久化存储 ──────────────────────────────────────────────

class PollStore:
    """投票数据的持久化存储"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / "polls.json"
        self._lock = asyncio.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self._file.exists():
            return {"polls": {}, "counter": 0, "closed": {}}
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"读取投票数据失败: {e}")
            return {"polls": {}, "counter": 0, "closed": {}}

    async def _save(self):
        async with self._lock:
            tmp = self._file.with_suffix(".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                tmp.replace(self._file)
            except OSError as e:
                logger.error(f"保存投票数据失败: {e}")

    def _next_id(self) -> int:
        self._data["counter"] += 1
        return self._data["counter"]

    def create_poll(
        self,
        question: str,
        options: list,
        creator_id: str,
        creator_name: str,
        session_id: str,
        multi: bool = False,
        anonymous: bool = False,
        expiry: Optional[int] = None,
    ) -> int:
        poll_id = self._next_id()
        expiry_time = None
        if expiry and expiry > 0:
            expiry_time = (datetime.now(timezone.utc).timestamp() + expiry * 60)

        self._data["polls"][str(poll_id)] = {
            "id": poll_id,
            "question": question,
            "options": [{"text": opt, "votes": []} for opt in options],
            "creator_id": creator_id,
            "creator_name": creator_name,
            "session_id": session_id,
            "multi": multi,
            "anonymous": anonymous,
            "expiry": expiry_time,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "total_votes": 0,
        }
        # Save to file
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._save())
            else:
                loop.run_until_complete(self._save())
        except:
            pass
        return poll_id

    def get_poll(self, poll_id: str) -> Optional[dict]:
        poll = self._data["polls"].get(poll_id)
        if not poll:
            return None
        # 检查是否过期
        if poll.get("expiry"):
            if datetime.now(timezone.utc).timestamp() > poll["expiry"]:
                return None
        return poll

    def get_active_polls(self, session_id: str = None) -> list:
        now_ts = datetime.now(timezone.utc).timestamp()
        polls = []
        for pid, poll in self._data["polls"].items():
            if poll.get("expiry") and now_ts > poll["expiry"]:
                continue
            if session_id and poll.get("session_id") != session_id:
                continue
            polls.append(poll)
        return sorted(polls, key=lambda p: p["id"], reverse=True)

    def vote(self, poll_id: str, option_index: int, user_id: str, user_name: str) -> tuple[bool, str]:
        poll = self._data["polls"].get(poll_id)
        if not poll:
            return False, "投票不存在或已过期"

        if poll.get("expiry"):
            if datetime.now(timezone.utc).timestamp() > poll["expiry"]:
                return False, "投票已截止"

        if option_index < 0 or option_index >= len(poll["options"]):
            return False, f"选项编号无效，有效范围 1-{len(poll['options'])}"

        option = poll["options"][option_index]

        # 检查是否已经投过这个选项
        if any(v["user_id"] == user_id for v in option["votes"]):
            return False, "你已经投过这个选项了"

        if not poll["multi"]:
            # 单选：检查是否投过其他选项，先移除
            for opt in poll["options"]:
                opt["votes"] = [v for v in opt["votes"] if v["user_id"] != user_id]

        # 投票
        option["votes"].append({
            "user_id": user_id,
            "user_name": user_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        poll["total_votes"] = sum(len(o["votes"]) for o in poll["options"])
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._save())
        except:
            pass
        return True, f"投票成功！你选择了: {option['text']}"

    def close_poll(self, poll_id: str, user_id: str) -> tuple[bool, str]:
        poll = self._data["polls"].get(poll_id)
        if not poll:
            return False, "投票不存在"
        if poll["creator_id"] != user_id:
            return False, "只有创建者才能关闭投票"
        # Move to closed
        self._data["closed"][poll_id] = self._data["polls"].pop(poll_id)
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._save())
        except:
            pass
        return True, "投票已关闭"

    def format_result(self, poll: dict) -> str:
        total = sum(len(o["votes"]) for o in poll["options"])
        lines = [
            f"📊 {poll['question']}",
            f"{'【多选】' if poll.get('multi') else '【单选】'} "
            f"{'🔒匿名' if poll.get('anonymous') else '👤公开'} "
            f"👥 {total} 票",
        ]

        if poll.get("expiry"):
            remaining = poll["expiry"] - datetime.now(timezone.utc).timestamp()
            if remaining > 0:
                mins = int(remaining / 60)
                lines.append(f"⏱ 剩余 {mins} 分钟")
            else:
                lines.append("⏱ 已截止")

        lines.append("")
        for i, opt in enumerate(poll["options"], 1):
            count = len(opt["votes"])
            bar_len = 20
            filled = int((count / max(total, 1)) * bar_len) if total > 0 else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            pct = (count / max(total, 1)) * 100
            lines.append(f"  {i}. {opt['text']}")
            lines.append(f"     {bar} {count} 票 ({pct:.1f}%)")

            if not poll.get("anonymous") and count > 0:
                voters = [v["user_name"] for v in opt["votes"]]
                lines.append(f"     👤 {', '.join(voters)}")

        lines.append(f"\n🆔 投票ID: {poll.get('id', '?')}")
        return "\n".join(lines)


# ── 插件主类 ──────────────────────────────────────────────

@register("astrbot_plugin_poll", "ownisjiang", "投票插件 - 在聊天中创建和参与投票", "1.0.0")
class PollPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        data_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "..", "data", "poll_plugin"
        )
        self.store = PollStore(data_dir)
        self._expiry_check_task: Optional[asyncio.Task] = None

    async def initialize(self):
        self._expiry_check_task = asyncio.create_task(self._expiry_loop())
        logger.info("投票插件已初始化")

    async def terminate(self):
        if self._expiry_check_task:
            self._expiry_check_task.cancel()
            try:
                await self._expiry_check_task
            except asyncio.CancelledError:
                pass
            self._expiry_check_task = None
        logger.info("投票插件已停止")

    async def _expiry_loop(self):
        """定期清理过期的投票（纯内存清理）"""
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    # ── 主命令 ──

    @filter.command("poll")
    async def poll(self, event: AstrMessageEvent, action: str = None):
        if not action:
            yield event.plain_result("📊 投票管理器\n输入 /poll help 查看帮助")
            return

        action = action.lower()

        if action == "help" or action == "h":
            yield event.plain_result(
                "📊 PollStar 投票插件\n"
                "━━━━━━━━━━━━━━━━\n"
                "命令:\n"
                "  /poll create \"问题\" \"选项1\" \"选项2\" ...  创建投票\n"
                "  /poll vote <id> <编号>                    参与投票\n"
                "  /poll result <id>                         查看结果\n"
                "  /poll list                                当前投票列表\n"
                "  /poll close <id>                          关闭投票\n\n"
                "参数:\n"
                "  --multi    多选 (默认单选)\n"
                "  --anon     匿名投票\n"
                "  --time N   截止时间（N 分钟后）\n\n"
                "示例:\n"
                "  /poll create \"今晚吃什么?\" \"火锅\" \"烤肉\" \"炒菜\"\n"
                "  /poll create \"选组长\" \"小明\" \"小红\" --anon\n"
                "  /poll create \"周末去哪\" \"海边\" \"爬山\" \"露营\" --time 60\n"
                "  /poll vote 1 2"
            )

        elif action == "create" or action == "c":
            async for result in self._cmd_create(event):
                yield result

        elif action == "vote" or action == "v":
            async for result in self._cmd_vote(event):
                yield result

        elif action in ("result", "res", "r"):
            async for result in self._cmd_result(event):
                yield result

        elif action in ("list", "ls"):
            async for result in self._cmd_list(event):
                yield result

        elif action in ("close", "end", "stop"):
            async for result in self._cmd_close(event):
                yield result

        else:
            yield event.plain_result(f"❌ 未知操作: {action}\n输入 /poll help 查看帮助")

    # ── 子命令实现 ──

    async def _cmd_create(self, event: AstrMessageEvent):
        text = event.message_str.strip()

        # 解析参数
        multi = " --multi" in text or " --多选" in text
        anonymous = " --anon" in text or " --匿名" in text
        expiry = None

        # 解析 --time N
        time_match = re.search(r'--time\s+(\d+)', text)
        if time_match:
            expiry = int(time_match.group(1))

        # 清理参数标记
        clean = text
        for flag in ["--multi", "--多选", "--anon", "--匿名"]:
            clean = clean.replace(flag, "")
        clean = re.sub(r'--time\s+\d+', '', clean)
        clean = clean.strip()

        # 解析引号内的内容
        parts = re.findall(r'"([^"]*)"', clean)
        if len(parts) < 3:
            yield event.plain_result(
                "❌ 格式错误！\n"
                "用法: /poll create \"问题\" \"选项1\" \"选项2\" ...\n"
                "示例: /poll create \"今晚吃什么?\" \"火锅\" \"烤肉\" \"炒菜\""
            )
            return

        question = parts[0]
        options = parts[1:]

        if len(options) < 2:
            yield event.plain_result("❌ 至少需要 2 个选项")
            return

        if len(options) > 10:
            yield event.plain_result("❌ 最多 10 个选项")
            return

        poll_id = self.store.create_poll(
            question=question,
            options=options,
            creator_id=event.get_sender_id(),
            creator_name=event.get_sender_name(),
            session_id=event.unified_msg_origin,
            multi=multi,
            anonymous=anonymous,
            expiry=expiry,
        )

        poll = self.store.get_poll(str(poll_id))
        msg = (
            f"📊 **投票已创建！**\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📌 {question}\n"
            f"{'✅ 多选' if multi else '🔘 单选'} | "
            f"{'🔒 匿名' if anonymous else '👤 公开'}\n"
        )
        if expiry:
            msg += f"⏱ {expiry} 分钟后截止\n"

        msg += "\n选项:\n"
        for i, opt in enumerate(options, 1):
            msg += f"  {i}. {opt}\n"

        msg += (
            f"\n参与投票: /poll vote {poll_id} <编号>\n"
            f"查看结果: /poll result {poll_id}\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: {poll_id}"
        )
        yield event.plain_result(msg)

    async def _cmd_vote(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        # /poll vote <id> <option_number>
        if len(parts) < 4:
            yield event.plain_result(
                "❌ 用法: /poll vote <投票ID> <选项编号>\n"
                "示例: /poll vote 1 2\n"
                "先用 /poll list 查看投票和选项"
            )
            return

        poll_id = parts[2]
        try:
            option_idx = int(parts[3]) - 1
        except ValueError:
            yield event.plain_result("❌ 选项编号必须是数字")
            return

        ok, msg = self.store.vote(
            poll_id,
            option_idx,
            event.get_sender_id(),
            event.get_sender_name(),
        )

        if ok:
            poll = self.store.get_poll(poll_id)
            if poll:
                yield event.plain_result(
                    f"✅ {msg}\n\n"
                    f"当前结果:\n{self.store.format_result(poll)}"
                )
            else:
                yield event.plain_result(f"✅ {msg}")
        else:
            yield event.plain_result(f"❌ {msg}")

    async def _cmd_result(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /poll result <投票ID>")
            return

        poll_id = parts[2]
        poll = self.store.get_poll(poll_id)
        if not poll:
            yield event.plain_result("❌ 投票不存在或已过期")
            return

        yield event.plain_result(self.store.format_result(poll))

    async def _cmd_list(self, event: AstrMessageEvent):
        polls = self.store.get_active_polls()
        if not polls:
            yield event.plain_result("📭 当前没有活跃的投票")
            return

        lines = ["📊 当前投票列表:\n"]
        for p in polls:
            total = sum(len(o["votes"]) for o in p["options"])
            multi = "多选" if p.get("multi") else "单选"
            expiry = ""
            if p.get("expiry"):
                remaining = p["expiry"] - datetime.now(timezone.utc).timestamp()
                if remaining > 0:
                    expiry = f" ⏱{int(remaining/60)}分钟"

            lines.append(
                f"  🆔 {p['id']} | {p['question'][:30]}\n"
                f"     {multi} | 👥{total}票{expiry}\n"
            )
        yield event.plain_result("".join(lines))

    async def _cmd_close(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("❌ 用法: /poll close <投票ID>")
            return

        poll_id = parts[2]
        ok, msg = self.store.close_poll(poll_id, event.get_sender_id())

        if ok:
            poll = self.store._data["closed"].get(poll_id)
            result = ""
            if poll:
                result = "\n\n最终结果:\n" + self.store.format_result(poll)
            yield event.plain_result(f"✅ {msg}{result}")
        else:
            yield event.plain_result(f"❌ {msg}")


# ── 别名命令 ──

@register("astrbot_plugin_poll_shortcut", "ownisjiang", "投票快捷命令", "1.0.0")
class PollShortcutPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)

    @filter.command("vote")
    async def vote_shortcut(self, event: AstrMessageEvent):
        """快捷投票: /vote <id> <编号>"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result(
                "用法: /vote <投票ID> <选项编号>\n"
                "查看投票: /poll list"
            )
            return
        # 重新构造为 /poll vote 命令
        event.message_str = f"/poll vote {parts[1]} {parts[2]}"

    @filter.command("投票")
    async def poll_cn(self, event: AstrMessageEvent):
        """中文命令别名"""
        text = event.message_str.strip()
        event.message_str = text.replace("投票", "poll", 1)
