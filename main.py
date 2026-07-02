import asyncio
import json
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
except ImportError:
    AiocqhttpMessageEvent = None


class BlacklistPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.name = "astrbot_plugin_Bans"
        data_root = Path(get_astrbot_data_path()) / "plugin_data" / self.name
        self.data_dir = data_root
        self.data_file = self.data_dir / "ban_list.json"
        self.ban_list = self._load_ban_list()
        self._scheduled_task = None
        logger.info(f"黑名单插件已加载，数据目录: {self.data_dir}")
        logger.info(f"当前黑名单: {self.ban_list}")

    def _load_ban_list(self) -> list:
        if not self.data_file.exists():
            return []
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载黑名单失败: {e}")
            return []

    def _save_ban_list(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.ban_list, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存黑名单失败: {e}")

    # ==================== NapCat API 调用 ====================

    async def _call_napcat_api(self, event: AstrMessageEvent, action: str, params: dict = None):
        if AiocqhttpMessageEvent is None:
            logger.error("AiocqhttpMessageEvent 不可用")
            return None
        if not isinstance(event, AiocqhttpMessageEvent):
            logger.error("当前事件不是来自 aiocqhttp 平台")
            return None
        if params is None:
            params = {}
        try:
            result = await event.bot.api.call_action(action, **params)
            return result
        except Exception as e:
            logger.error(f"调用 NapCat API 失败: {action}, 错误: {e}")
            return None

    async def _get_group_list(self, event: AstrMessageEvent) -> list:
        result = await self._call_napcat_api(event, "get_group_list")
        if result is None:
            return None
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and result.get("status") == "ok" and "data" in result:
            return result.get("data", [])
        return None

    async def _get_group_member_list(self, event: AstrMessageEvent, group_id: str) -> list:
        result = await self._call_napcat_api(event, "get_group_member_list", {"group_id": group_id})
        if result is None:
            return None
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and result.get("status") == "ok" and "data" in result:
            return result.get("data", [])
        return None

    async def _get_friend_list(self, event: AstrMessageEvent) -> list:
        result = await self._call_napcat_api(event, "get_friend_list")
        if result is None:
            return None
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and result.get("status") == "ok" and "data" in result:
            return result.get("data", [])
        return None

    async def _leave_group(self, event: AstrMessageEvent, group_id: str):
        await self._call_napcat_api(event, "set_group_leave", {"group_id": group_id, "is_dismiss": False})

    async def _delete_friend(self, event: AstrMessageEvent, user_id: str):
        await self._call_napcat_api(event, "delete_friend", {"user_id": user_id})

    async def _send_group_message(self, event: AstrMessageEvent, group_id: str, text: str):
        """向指定群发送文本消息"""
        await self._call_napcat_api(event, "send_group_msg", {"group_id": group_id, "message": text})

    # ==================== 指令 ====================

    @filter.command("bans")
    async def add_ban(self, event: AstrMessageEvent, qq: str):
        if not qq.isdigit():
            yield event.plain_result("❌ 请提供有效的 QQ 号，例如：/bans 123456789")
            return
        if qq in self.ban_list:
            yield event.plain_result(f"⚠️ {qq} 已在黑名单中")
            return
        try:
            await self._delete_friend(event, qq)
        except Exception as e:
            logger.error(f"删除好友失败: {e}")
            yield event.plain_result(f"❌ 删除好友失败: {e}")
            return
        self.ban_list.append(qq)
        self._save_ban_list()
        yield event.plain_result(f"✅ {qq} 已被加入黑名单")

    @filter.command("debans")
    async def remove_ban(self, event: AstrMessageEvent, qq: str):
        if qq not in self.ban_list:
            yield event.plain_result(f"⚠️ {qq} 不在黑名单中")
            return
        self.ban_list.remove(qq)
        self._save_ban_list()
        yield event.plain_result(f"✅ {qq} 已从黑名单中移除")

    @filter.command("banslist")
    async def list_bans(self, event: AstrMessageEvent):
        if not self.ban_list:
            yield event.plain_result("📭 黑名单为空")
            return
        count = len(self.ban_list)
        qq_list = "\n".join([f"- {qq}" for qq in self.ban_list])
        yield event.plain_result(f"📋 黑名单列表（共 {count} 人）：\n{qq_list}")

    @filter.command("CAllbans")
    async def check_all(self, event: AstrMessageEvent):
        if not self.ban_list:
            yield event.plain_result("📭 黑名单为空，无需检查")
            return

        # 先回复开始信息（被动回复）
        yield event.plain_result("🔄 正在检查所有群聊和好友列表...")

        # ---- 第一步：扫描群，收集需要退出的群 ----
        groups = await self._get_group_list(event)
        if groups is None:
            yield event.plain_result("❌ 获取群列表失败")
            return

        pending_groups = []  # 存储 (group_id, group_name, banned_qq_list)
        for group in groups:
            group_id = str(group.get("group_id"))
            group_name = group.get("group_name", group_id)
            try:
                members = await self._get_group_member_list(event, group_id)
                if members is None:
                    logger.warning(f"获取群 {group_id} 成员列表失败")
                    continue
                banned_members = [m for m in members if str(m.get("user_id")) in self.ban_list]
                if banned_members:
                    banned_qq_list = [str(m.get("user_id")) for m in banned_members]
                    pending_groups.append((group_id, group_name, banned_qq_list))
            except Exception as e:
                logger.error(f"扫描群 {group_id} 异常: {e}")

        # ---- 第二步：生成汇总消息并发送（在退群前） ----
        if pending_groups:
            summary_lines = [f"📋 发现 {len(pending_groups)} 个群含有黑名单成员："]
            for idx, (gid, gname, banned_list) in enumerate(pending_groups, 1):
                summary_lines.append(f"{idx}. {gname}({gid}) - 黑名单: {', '.join(banned_list)}")
            summary = "\n".join(summary_lines)
            # 发送汇总消息（当前会话尚未退群，可以发送）
            await event.send(MessageChain([Plain(summary)]))
        else:
            await event.send(MessageChain([Plain("✅ 所有群均无黑名单成员")]))

        # ---- 第三步：执行退群操作 ----
        total_kicked = 0
        for group_id, group_name, banned_qq_list in pending_groups:
            try:
                # 发送退群通知（可能失败，不影响）
                notify_text = f"🚫 本群检测到黑名单用户（{', '.join(banned_qq_list)}），机器人将自动退出。"
                try:
                    await self._send_group_message(event, group_id, notify_text)
                    logger.info(f"已向群 {group_name}({group_id}) 发送退群通知")
                except Exception as e:
                    logger.warning(f"向群 {group_id} 发送通知失败: {e}")
                # 退群
                await self._leave_group(event, group_id)
                logger.info(f"已退出群 {group_name}({group_id})，因检测到黑名单用户 {', '.join(banned_qq_list)}")
                total_kicked += 1
            except Exception as e:
                logger.error(f"退出群 {group_id} 失败: {e}")

        # ---- 第四步：检查好友列表并删除 ----
        total_deleted = 0
        friends = await self._get_friend_list(event)
        if friends is None:
            logger.error("获取好友列表失败")
        else:
            for friend in friends:
                user_id = str(friend.get("user_id"))
                if user_id in self.ban_list:
                    try:
                        await self._delete_friend(event, user_id)
                        logger.info(f"已删除好友 {user_id}")
                        total_deleted += 1
                    except Exception as e:
                        logger.error(f"删除好友 {user_id} 失败: {e}")

        # 发送最终汇总（可选，但已发送过主要汇总，这里可以补充好友删除结果）
        final_msg = f"✅ 操作完成\n- 退出群组：{total_kicked} 个\n- 删除好友：{total_deleted} 个"
        # 由于可能已经退出了当前群，用私聊发送最终结果
        await self._send_private_result(event, final_msg)

    @filter.command("Tbans")
    async def schedule_check(self, event: AstrMessageEvent, h: str):
        if not h.isdigit() or int(h) <= 0:
            yield event.plain_result("❌ 请提供有效的小时数，例如：/Tbans 2")
            return
        interval_hours = int(h)
        if self._scheduled_task and not self._scheduled_task.done():
            self._scheduled_task.cancel()
            yield event.plain_result(f"⏹️ 已取消之前的定时任务")
        self._scheduled_task = asyncio.create_task(
            self._scheduled_check_loop(event, interval_hours)
        )
        yield event.plain_result(f"⏰ 定时任务已启动，每 {interval_hours} 小时检查一次")

    # ==================== 辅助发送私聊 ====================

    async def _send_private_result(self, event: AstrMessageEvent, text: str):
        """尝试给命令发起者发送私聊消息"""
        user_id = event.get_sender_id()
        private_umo = {
            "platform": event.get_platform_name(),
            "session_id": str(user_id),
            "session_type": "private"
        }
        try:
            await self.context.send_message(private_umo, MessageChain([Plain(text)]))
            logger.info(f"已向 {user_id} 发送私聊结果")
        except Exception as e:
            logger.warning(f"发送私聊结果失败: {e}")
            # 若私聊失败，尝试发送到原会话（可能已退群）
            try:
                await self.context.send_message(event.unified_msg_origin, MessageChain([Plain(text)]))
            except Exception as e2:
                logger.error(f"发送结果完全失败: {e2}")

    # ==================== 定时任务循环 ====================

    async def _scheduled_check_loop(self, event: AstrMessageEvent, interval_hours: int):
        # 保存发起者信息（用于私聊发送汇总）
        user_id = event.get_sender_id()
        platform = event.get_platform_name()
        private_umo = {
            "platform": platform,
            "session_id": str(user_id),
            "session_type": "private"
        }

        while True:
            try:
                await asyncio.sleep(interval_hours * 3600)
                if not self.ban_list:
                    logger.info("黑名单为空，跳过定时检查")
                    continue

                logger.info("定时检查：开始检查所有群和好友")
                # ---- 扫描群 ----
                groups = await self._get_group_list(event)
                pending_groups = []
                if groups is None:
                    logger.error("定时检查：获取群列表失败")
                else:
                    for group in groups:
                        group_id = str(group.get("group_id"))
                        group_name = group.get("group_name", group_id)
                        try:
                            members = await self._get_group_member_list(event, group_id)
                            if members is None:
                                continue
                            banned_members = [m for m in members if str(m.get("user_id")) in self.ban_list]
                            if banned_members:
                                banned_qq_list = [str(m.get("user_id")) for m in banned_members]
                                pending_groups.append((group_id, group_name, banned_qq_list))
                        except Exception as e:
                            logger.error(f"定时扫描群 {group_id} 异常: {e}")

                # ---- 发送汇总（私聊） ----
                if pending_groups:
                    summary_lines = [f"📋 定时任务：发现 {len(pending_groups)} 个群含有黑名单成员："]
                    for idx, (gid, gname, banned_list) in enumerate(pending_groups, 1):
                        summary_lines.append(f"{idx}. {gname}({gid}) - 黑名单: {', '.join(banned_list)}")
                    summary = "\n".join(summary_lines)
                    try:
                        await self.context.send_message(private_umo, MessageChain([Plain(summary)]))
                    except Exception as e:
                        logger.error(f"发送定时汇总私聊失败: {e}")
                else:
                    try:
                        await self.context.send_message(private_umo, MessageChain([Plain("✅ 定时检查：所有群均无黑名单成员")]))
                    except Exception:
                        pass

                # ---- 执行退群 ----
                total_kicked = 0
                for group_id, group_name, banned_qq_list in pending_groups:
                    try:
                        notify_text = f"🚫 定时检查：本群检测到黑名单用户（{', '.join(banned_qq_list)}），机器人将自动退出。"
                        try:
                            await self._send_group_message(event, group_id, notify_text)
                        except Exception:
                            pass
                        await self._leave_group(event, group_id)
                        logger.info(f"定时检查：已退出群 {group_name}({group_id})，因黑名单用户 {', '.join(banned_qq_list)}")
                        total_kicked += 1
                    except Exception as e:
                        logger.error(f"定时退群 {group_id} 失败: {e}")

                # ---- 检查好友 ----
                total_deleted = 0
                friends = await self._get_friend_list(event)
                if friends is None:
                    logger.error("定时检查：获取好友列表失败")
                else:
                    for friend in friends:
                        user_id_friend = str(friend.get("user_id"))
                        if user_id_friend in self.ban_list:
                            try:
                                await self._delete_friend(event, user_id_friend)
                                logger.info(f"定时检查：已删除好友 {user_id_friend}")
                                total_deleted += 1
                            except Exception as e:
                                logger.error(f"定时检查删除好友 {user_id_friend} 失败: {e}")

                # 发送最终结果（私聊）
                if total_kicked > 0 or total_deleted > 0:
                    final_msg = f"⏰ 定时任务完成\n- 退出群组：{total_kicked} 个\n- 删除好友：{total_deleted} 个"
                    try:
                        await self.context.send_message(private_umo, MessageChain([Plain(final_msg)]))
                    except Exception as e:
                        logger.error(f"发送定时最终结果失败: {e}")

            except asyncio.CancelledError:
                logger.info("定时任务已取消")
                break
            except Exception as e:
                logger.error(f"定时任务执行异常: {e}")

    async def terminate(self):
        if self._scheduled_task and not self._scheduled_task.done():
            self._scheduled_task.cancel()
            logger.info("定时任务已取消")
        logger.info("黑名单插件已卸载")