import asyncio
import json
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )
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
        self._fk_scan_result = None
        logger.info(f"黑名单插件已加载，数据目录: {self.data_dir}")
        logger.info(f"当前黑名单: {self.ban_list}")

    # ==================== 数据持久化 ====================

    def _load_ban_list(self) -> list:
        if not self.data_file.exists():
            return []
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 迁移旧格式（纯字符串列表 -> 字典列表）
            if isinstance(data, list) and data and isinstance(data[0], str):
                new_data = [{"qq": qq, "reason": ""} for qq in data]
                logger.info("已自动迁移旧版黑名单格式")
                return new_data
            return data
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

    def _get_banned_qq_set(self) -> set:
        return {item["qq"] for item in self.ban_list}

    def _get_reason(self, qq: str) -> str:
        for item in self.ban_list:
            if item["qq"] == qq:
                return item.get("reason", "")
        return ""

    # ==================== NapCat API 调用 ====================

    async def _call_napcat_api(
        self, event: AstrMessageEvent, action: str, params: dict = None
    ):
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
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and "data" in result
        ):
            return result.get("data", [])
        return None

    async def _get_group_member_list(
        self, event: AstrMessageEvent, group_id: str
    ) -> list:
        result = await self._call_napcat_api(
            event, "get_group_member_list", {"group_id": group_id}
        )
        if result is None:
            return None
        if isinstance(result, list):
            return result
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and "data" in result
        ):
            return result.get("data", [])
        return None

    async def _get_friend_list(self, event: AstrMessageEvent) -> list:
        result = await self._call_napcat_api(event, "get_friend_list")
        if result is None:
            return None
        if isinstance(result, list):
            return result
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and "data" in result
        ):
            return result.get("data", [])
        return None

    async def _leave_group(self, event: AstrMessageEvent, group_id: str):
        await self._call_napcat_api(
            event, "set_group_leave", {"group_id": group_id, "is_dismiss": False}
        )

    async def _delete_friend(self, event: AstrMessageEvent, user_id: str):
        await self._call_napcat_api(event, "delete_friend", {"user_id": user_id})

    async def _send_group_message(
        self, event: AstrMessageEvent, group_id: str, text: str
    ):
        await self._call_napcat_api(
            event, "send_group_msg", {"group_id": group_id, "message": text}
        )

    # ==================== 通用扫描 ====================

    async def _scan_all(self, event: AstrMessageEvent):
        banned_set = self._get_banned_qq_set()
        groups_with_bans = []
        friends_banned = []

        groups = await self._get_group_list(event)
        if groups is not None:
            for group in groups:
                group_id = str(group.get("group_id"))
                group_name = group.get("group_name", group_id)
                try:
                    members = await self._get_group_member_list(event, group_id)
                    if members is None:
                        continue
                    banned_members = [
                        {
                            "qq": str(m.get("user_id")),
                            "reason": self._get_reason(str(m.get("user_id"))),
                        }
                        for m in members
                        if str(m.get("user_id")) in banned_set
                    ]
                    if banned_members:
                        groups_with_bans.append(
                            {
                                "group_id": group_id,
                                "group_name": group_name,
                                "banned_users": banned_members,
                            }
                        )
                except Exception as e:
                    logger.error(f"扫描群 {group_id} 异常: {e}")

        friends = await self._get_friend_list(event)
        if friends is not None:
            for friend in friends:
                qq = str(friend.get("user_id"))
                if qq in banned_set:
                    friends_banned.append({"qq": qq, "reason": self._get_reason(qq)})

        return groups_with_bans, friends_banned

    # ==================== 辅助发送私聊 ====================

    async def _send_private_result(self, event: AstrMessageEvent, text: str):
        user_id = event.get_sender_id()
        private_umo = {
            "platform": event.get_platform_name(),
            "session_id": str(user_id),
            "session_type": "private",
        }
        try:
            await self.context.send_message(private_umo, MessageChain([Plain(text)]))
            logger.info(f"已向 {user_id} 发送私聊结果")
        except Exception as e:
            logger.warning(f"发送私聊结果失败: {e}")
            try:
                await self.context.send_message(
                    event.unified_msg_origin, MessageChain([Plain(text)])
                )
            except Exception as e2:
                logger.error(f"发送结果完全失败: {e2}")

    # ==================== 命令（仅管理员） ====================

    @filter.command("bans")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def add_ban(self, event: AstrMessageEvent):
        """添加黑名单，用法：/bans <QQ号> [理由]"""
        raw = event.message_str.strip()
        if raw.startswith("/bans"):
            raw = raw[len("/bans") :].strip()
        elif raw.startswith("bans"):
            raw = raw[len("bans") :].strip()
        if not raw:
            yield event.plain_result("用法：/bans <QQ号> [理由]")
            return
        parts = raw.split(maxsplit=1)
        qq = parts[0]
        reason = parts[1] if len(parts) > 1 else ""
        if not qq.isdigit():
            yield event.plain_result("请提供有效的 QQ 号")
            return

        for item in self.ban_list:
            if item["qq"] == qq:
                yield event.plain_result(
                    f"{qq} 已在黑名单中，理由：{item.get('reason', '无')}"
                )
                return

        try:
            await self._delete_friend(event, qq)
            yield event.plain_result(f"已删除好友 {qq}")
        except Exception as e:
            logger.error(f"删除好友失败: {e}")
            yield event.plain_result(f"删除好友失败: {e}，但已加入黑名单")

        self.ban_list.append({"qq": qq, "reason": reason})
        self._save_ban_list()
        yield event.plain_result(
            f"{qq} 已被加入黑名单" + (f"，理由：{reason}" if reason else "")
        )

    @filter.command("debans")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def remove_ban(self, event: AstrMessageEvent, qq: str):
        """移除黑名单，用法：/debans <QQ号>"""
        if not qq.isdigit():
            yield event.plain_result("请提供有效的 QQ 号")
            return
        removed = False
        for i, item in enumerate(self.ban_list):
            if item["qq"] == qq:
                self.ban_list.pop(i)
                removed = True
                break
        if not removed:
            yield event.plain_result(f"{qq} 不在黑名单中")
            return
        self._save_ban_list()
        yield event.plain_result(f"{qq} 已从黑名单中移除")

    @filter.command("banslist")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_bans(self, event: AstrMessageEvent):
        """查看黑名单列表"""
        if not self.ban_list:
            yield event.plain_result("黑名单为空")
            return
        lines = [f"黑名单列表（共 {len(self.ban_list)} 人）："]
        for idx, item in enumerate(self.ban_list, 1):
            qq = item["qq"]
            reason = item.get("reason", "")
            line = f"{idx}. {qq}" + (f" - 理由：{reason}" if reason else "")
            lines.append(line)
        yield event.plain_result("\n".join(lines))

    @filter.command("CAllbans")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def check_all(self, event: AstrMessageEvent):
        """检查所有群和好友，自动退群并删除好友"""
        if not self.ban_list:
            yield event.plain_result("黑名单为空，无需检查")
            return

        yield event.plain_result("正在检查所有群聊和好友列表...")
        groups_with_bans, friends_banned = await self._scan_all(event)

        if not groups_with_bans and not friends_banned:
            yield event.plain_result("所有群和好友均无黑名单成员")
            return

        summary_lines = []
        if groups_with_bans:
            summary_lines.append(f"发现 {len(groups_with_bans)} 个群含有黑名单成员：")
            for g in groups_with_bans:
                qq_list = ", ".join([u["qq"] for u in g["banned_users"]])
                summary_lines.append(
                    f"  - {g['group_name']}({g['group_id']}) 黑名单: {qq_list}"
                )
        if friends_banned:
            qq_list = ", ".join([f["qq"] for f in friends_banned])
            summary_lines.append(f"黑名单好友: {qq_list}")
        summary = "\n".join(summary_lines)
        await event.send(MessageChain([Plain(summary)]))

        total_kicked = 0
        for g in groups_with_bans:
            group_id = g["group_id"]
            group_name = g["group_name"]
            qq_list = ", ".join([u["qq"] for u in g["banned_users"]])
            try:
                notify_text = f"本群检测到黑名单用户（{qq_list}），机器人将自动退出。"
                await self._send_group_message(event, group_id, notify_text)
                logger.info(f"已向群 {group_name}({group_id}) 发送退群通知")
            except Exception as e:
                logger.warning(f"向群 {group_id} 发送通知失败: {e}")
            try:
                await self._leave_group(event, group_id)
                logger.info(
                    f"已退出群 {group_name}({group_id})，因黑名单用户 {qq_list}"
                )
                total_kicked += 1
            except Exception as e:
                logger.error(f"退出群 {group_id} 失败: {e}")

        total_deleted = 0
        for f in friends_banned:
            qq = f["qq"]
            try:
                await self._delete_friend(event, qq)
                logger.info(f"已删除好友 {qq}")
                total_deleted += 1
            except Exception as e:
                logger.error(f"删除好友 {qq} 失败: {e}")

        final_msg = (
            f"操作完成\n- 退出群组：{total_kicked} 个\n- 删除好友：{total_deleted} 个"
        )
        await self._send_private_result(event, final_msg)

    @filter.command("ckbans")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def check_only(self, event: AstrMessageEvent):
        """仅检查，不退群不删好友"""
        if not self.ban_list:
            yield event.plain_result("黑名单为空")
            return

        yield event.plain_result("正在扫描所有群聊和好友...")
        groups_with_bans, friends_banned = await self._scan_all(event)

        if not groups_with_bans and not friends_banned:
            yield event.plain_result("所有群和好友均无黑名单成员")
            return

        lines = ["扫描结果（仅报告，未执行任何操作）："]
        if groups_with_bans:
            lines.append(f"群聊（共 {len(groups_with_bans)} 个群含有黑名单）：")
            for g in groups_with_bans:
                users = ", ".join(
                    [f"{u['qq']}({u['reason'] or '无理由'})" for u in g["banned_users"]]
                )
                lines.append(f"  • {g['group_name']}({g['group_id']}) -> {users}")
        if friends_banned:
            users = ", ".join(
                [f"{f['qq']}({f['reason'] or '无理由'})" for f in friends_banned]
            )
            lines.append(f"好友：{users}")
        yield event.plain_result("\n".join(lines))

    @filter.command("fkbans")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def notify_banned_groups(self, event: AstrMessageEvent):
        """扫描群聊，保存结果，供 /bansconfirm 发送通知"""
        if not self.ban_list:
            yield event.plain_result("黑名单为空")
            return

        yield event.plain_result("正在扫描群聊...")
        groups_with_bans, _ = await self._scan_all(event)

        if not groups_with_bans:
            yield event.plain_result("所有群均无黑名单成员")
            self._fk_scan_result = None
            return

        self._fk_scan_result = groups_with_bans

        lines = [f"发现 {len(groups_with_bans)} 个群含有黑名单成员："]
        for idx, g in enumerate(groups_with_bans, 1):
            users = ", ".join(
                [f"{u['qq']}({u['reason'] or '无理由'})" for u in g["banned_users"]]
            )
            lines.append(f"{idx}. {g['group_name']}({g['group_id']}) -> {users}")
        lines.append("\n如需向这些群发送通知，请执行：/bansconfirm")
        yield event.plain_result("\n".join(lines))

    @filter.command("bansconfirm")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def confirm_send_notification(self, event: AstrMessageEvent):
        """确认发送通知至 /fkbans 扫描出的含有黑名单的群（不退群）"""
        if self._fk_scan_result is None:
            yield event.plain_result("没有待发送的扫描结果，请先执行 /fkbans 扫描")
            return

        groups_with_bans = self._fk_scan_result
        success_count = 0

        for g in groups_with_bans:
            group_id = g["group_id"]
            group_name = g["group_name"]
            users_info = ", ".join(
                [
                    f"{u['qq']}(理由：{u['reason'] or '未提供'})"
                    for u in g["banned_users"]
                ]
            )
            notify_text = f"本群存在本机器人黑名单用户：{users_info}，若不处理，机器人将自动退出。\n（此为检查通知）"
            try:
                await self._send_group_message(event, group_id, notify_text)
                logger.info(f"已向群 {group_name}({group_id}) 发送黑名单通知")
                success_count += 1
            except Exception as e:
                logger.error(f"向群 {group_id} 发送通知失败: {e}")

        self._fk_scan_result = None
        yield event.plain_result(
            f"已向 {success_count}/{len(groups_with_bans)} 个群发送通知"
        )

    @filter.command("Tbans")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def schedule_check(self, event: AstrMessageEvent, h: str):
        """设置定时检查（小时），用法：/Tbans <小时数>"""
        if not h.isdigit() or int(h) <= 0:
            yield event.plain_result("请提供有效的小时数，例如：/Tbans 2")
            return
        interval_hours = int(h)
        if self._scheduled_task and not self._scheduled_task.done():
            self._scheduled_task.cancel()
            yield event.plain_result("已取消之前的定时任务")
        self._scheduled_task = asyncio.create_task(
            self._scheduled_check_loop(event, interval_hours)
        )
        yield event.plain_result(f"定时任务已启动，每 {interval_hours} 小时检查一次")

    async def _scheduled_check_loop(self, event: AstrMessageEvent, interval_hours: int):
        user_id = event.get_sender_id()
        platform = event.get_platform_name()
        private_umo = {
            "platform": platform,
            "session_id": str(user_id),
            "session_type": "private",
        }

        while True:
            try:
                await asyncio.sleep(interval_hours * 3600)
                if not self.ban_list:
                    logger.info("黑名单为空，跳过定时检查")
                    continue

                logger.info("定时检查：开始检查所有群和好友")
                groups_with_bans, friends_banned = await self._scan_all(event)

                if groups_with_bans or friends_banned:
                    lines = ["定时检查发现以下黑名单分布："]
                    if groups_with_bans:
                        lines.append("群聊：")
                        for g in groups_with_bans:
                            users = ", ".join(
                                [
                                    f"{u['qq']}({u['reason'] or '无理由'})"
                                    for u in g["banned_users"]
                                ]
                            )
                            lines.append(
                                f"  • {g['group_name']}({g['group_id']}) -> {users}"
                            )
                    if friends_banned:
                        users = ", ".join(
                            [
                                f"{f['qq']}({f['reason'] or '无理由'})"
                                for f in friends_banned
                            ]
                        )
                        lines.append(f"好友：{users}")
                    try:
                        await self.context.send_message(
                            private_umo, MessageChain([Plain("\n".join(lines))])
                        )
                    except Exception as e:
                        logger.error(f"发送定时检查结果私聊失败: {e}")

                    total_kicked = 0
                    for g in groups_with_bans:
                        group_id = g["group_id"]
                        group_name = g["group_name"]
                        qq_list = ", ".join([u["qq"] for u in g["banned_users"]])
                        try:
                            notify_text = f"定时检查：本群检测到黑名单用户（{qq_list}），机器人将自动退出。"
                            await self._send_group_message(event, group_id, notify_text)
                        except Exception:
                            pass
                        try:
                            await self._leave_group(event, group_id)
                            logger.info(
                                f"定时检查：已退出群 {group_name}({group_id})，因黑名单用户 {qq_list}"
                            )
                            total_kicked += 1
                        except Exception as e:
                            logger.error(f"定时退群 {group_id} 失败: {e}")

                    total_deleted = 0
                    for f in friends_banned:
                        qq = f["qq"]
                        try:
                            await self._delete_friend(event, qq)
                            logger.info(f"定时检查：已删除好友 {qq}")
                            total_deleted += 1
                        except Exception as e:
                            logger.error(f"定时检查删除好友 {qq} 失败: {e}")

                    if total_kicked > 0 or total_deleted > 0:
                        final_msg = f"定时任务完成\n- 退出群组：{total_kicked} 个\n- 删除好友：{total_deleted} 个"
                        try:
                            await self.context.send_message(
                                private_umo, MessageChain([Plain(final_msg)])
                            )
                        except Exception as e:
                            logger.error(f"发送定时最终结果失败: {e}")
                else:
                    try:
                        await self.context.send_message(
                            private_umo,
                            MessageChain(
                                [Plain("定时检查：所有群和好友均无黑名单成员")]
                            ),
                        )
                    except Exception:
                        pass

            except asyncio.CancelledError:
                logger.info("定时任务已取消")
                break
            except Exception as e:
                logger.error(f"定时任务执行异常: {e}")

    # ==================== 生命周期 ====================

    async def terminate(self):
        if self._scheduled_task and not self._scheduled_task.done():
            self._scheduled_task.cancel()
            logger.info("定时任务已取消")
        logger.info("黑名单插件已卸载")
