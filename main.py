import asyncio
import re
from typing import Optional, List, Dict, Any

from astrbot.api import logger
from astrbot.api.event import filter, AiocqhttpEvent
from astrbot.api.star import Context, Star
from astrbot.core.message.components import Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)


class MonitorPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 监听群 -> 被监听群
        self.monitor_map: Dict[int, int] = {}

    # ---------- 工具函数 ----------
    def extract_group_ids(self, text: str) -> List[int]:
        """从文本中提取群号"""
        try:
            return [int(gid) for gid in re.findall(r"\d{6,10}", text)]
        except (ValueError, AttributeError) as e:
            logger.warning(f"提取群号失败: {e}")
            return []

    def get_group_ids(self, event: AiocqhttpMessageEvent) -> List[int]:
        """获取事件相关的群号"""
        try:
            reply_seg = next(
                (seg for seg in event.get_messages() if isinstance(seg, Reply)), None
            )
            ref_text = reply_seg.message_str if reply_seg else ""
            return self.extract_group_ids(ref_text or event.message_str)
        except Exception as e:
            logger.warning(f"获取群号异常: {e}")
            return []

    def extract_image_urls(self, message_data: Dict[str, Any]) -> List[str]:
        """从消息数据中提取图片URL
        
        支持 CQHTTP 消息格式:
        - 消息链格式: [{"type": "image", "data": {"url": "..."}}]
        - 字符串格式: "[CQ:image,file=...]"
        """
        image_urls = []
        try:
            if "message" not in message_data:
                return image_urls

            msg_content = message_data["message"]
            
            # 处理消息链格式（列表）
            if isinstance(msg_content, list):
                for segment in msg_content:
                    if isinstance(segment, dict) and segment.get("type") == "image":
                        data = segment.get("data", {})
                        url = data.get("url") or data.get("file")
                        if url:
                            image_urls.append(url)
            
            # 处理 CQ 码格式（字符串）
            elif isinstance(msg_content, str):
                cq_image_pattern = r"\[CQ:image,file=([^\]]+)\]"
                matches = re.findall(cq_image_pattern, msg_content)
                image_urls.extend(matches)
                
        except Exception as e:
            logger.warning(f"提取图片URL失败: {e}")
        
        return image_urls

    async def build_forward_nodes(
        self,
        messages: List[Dict[str, Any]],
        user_id: Optional[int] = None,
        include_images: bool = True,
    ) -> List[Dict[str, Any]]:
        """构建转发节点，支持文本和图片"""
        nodes = []
        
        for msg in messages:
            try:
                # 检查必要字段
                if "sender" not in msg or "message" not in msg:
                    logger.debug("消息缺少必要字段，跳过")
                    continue
                
                sender = msg["sender"]
                if user_id and sender.get("user_id") != user_id:
                    continue
                
                content = msg["message"]
                
                # 提取图片
                if include_images:
                    image_urls = self.extract_image_urls(msg)
                    
                    # 如果有图片，转换为消息链格式
                    if image_urls:
                        message_chain = []
                        
                        # 添加文本内容
                        if isinstance(content, str) and content.strip():
                            message_chain.append({
                                "type": "text",
                                "data": {"text": content}
                            })
                        
                        # 添加图片
                        for url in image_urls:
                            message_chain.append({
                                "type": "image",
                                "data": {"url": url}
                            })
                        
                        content = message_chain
                
                # 构建节点
                node = {
                    "type": "node",
                    "data": {
                        "name": sender.get("nickname", "未知用户"),
                        "uin": sender.get("user_id", 0),
                        "content": content,
                    },
                }
                nodes.append(node)
                
            except Exception as e:
                logger.warning(f"处理消息节点失败: {e}")
                continue
        
        return nodes

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("抽查")
    async def check_messages(
        self,
        event: AiocqhttpMessageEvent,
        group_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ):
        """抽查 [群号] [数量] - 查看群聊记录（支持图片）"""
        try:
            args = event.message_str.split()
            count = next(
                (int(arg) for arg in args if arg.isdigit() and int(arg) < 1000), 20
            )

            group_ids = [group_id] if group_id else self.get_group_ids(event)
            if not group_ids:
                yield event.plain_result("未指定要抽查的群号")
                return
            
            target_group = int(event.get_group_id())
            target_user = int(event.get_sender_id())
            
            async def check_single(gid: int):
                try:
                    result = await event.bot.get_group_msg_history(
                        group_id=gid, count=count
                    )
                    nodes = await self.build_forward_nodes(
                        result.get("messages", []),
                        user_id,
                        include_images=True
                    )
                    
                    if not nodes:
                        logger.warning(f"群 {gid} 没有可转发的消息")
                        return
                    
                    if target_group:
                        await event.bot.send_group_forward_msg(
                            group_id=target_group, messages=nodes
                        )
                    else:
                        await event.bot.send_private_forward_msg(
                            user_id=target_user, messages=nodes
                        )
                except Exception as e:
                    logger.error(f"抽查群({gid})失败: {e}")

            await asyncio.gather(*(check_single(gid) for gid in group_ids))
            yield event.plain_result("抽查完成")
            
        except Exception as e:
            logger.error(f"抽查命令执行失败: {e}")
            yield event.plain_result(f"执行失败: {str(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("回复")
    async def reply(self, event: AiocqhttpMessageEvent):
        """(引用群号)回复 [内容] - 向指定群发送消息"""
        try:
            msg = event.message_str.removeprefix("回复 ").strip()
            if not msg:
                yield event.plain_result("请在回复后面加上要回复的内容")
                return

            group_ids = self.get_group_ids(event)
            if not group_ids:
                yield event.plain_result("未指定要回复的群号")
                return

            success_count = 0
            for gid in group_ids:
                try:
                    await event.bot.send_group_msg(group_id=gid, message=msg)
                    success_count += 1
                except Exception as e:
                    logger.error(f"向群({gid})发送消息失败: {e}")
            
            yield event.plain_result(f"已将消息转发到 {success_count}/{len(group_ids)} 个群")
            
        except Exception as e:
            logger.error(f"回复命令执行失败: {e}")
            yield event.plain_result(f"执行失败: {str(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("监听")
    async def start_monitor(self, event: AiocqhttpMessageEvent):
        """监听 [群号] - 开始监听目标群的消息"""
        try:
            group_ids = self.get_group_ids(event)
            if not group_ids:
                yield event.plain_result("未指定要监听的群号")
                return

            monitor_group = int(event.get_group_id())
            target_group = group_ids[0]
            
            self.monitor_map[monitor_group] = target_group
            yield event.plain_result(f"开始监听群 {target_group}")
            
        except Exception as e:
            logger.error(f"监听命令执行失败: {e}")
            yield event.plain_result(f"执行失败: {str(e)}")
        finally:
            event.stop_event()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消监听")
    async def stop_monitor(self, event: AiocqhttpMessageEvent):
        """取消监听 - 停止监听目标群"""
        try:
            monitor_group = int(event.get_group_id())
            if monitor_group in self.monitor_map:
                target = self.monitor_map[monitor_group]
                del self.monitor_map[monitor_group]
                yield event.plain_result(f"已取消对群 {target} 的监听")
            else:
                yield event.plain_result("当前群未设置监听")
                
        except Exception as e:
            logger.error(f"取消监听命令执行失败: {e}")
            yield event.plain_result(f"执行失败: {str(e)}")
        finally:
            event.stop_event()

    # ⚠️ 注意：实时转发需要使用正确的事件监听方法
    # 以下是备选实现，具体取决于 astrbot 版本
    @filter.group_message()
    async def on_group_message(self, event: AiocqhttpMessageEvent):
        """实时转发被监听群的消息"""
        try:
            if not isinstance(event, AiocqhttpMessageEvent):
                return

            source_group = int(event.get_group_id())
            
            # 检查是否是被监听的群
            for monitor_group, target_group in self.monitor_map.items():
                if source_group == target_group:
                    try:
                        # 获取消息数据
                        sender = event.get_sender()
                        msg_data = {
                            "sender": {
                                "user_id": sender.get("user_id") if isinstance(sender, dict) else int(event.get_sender_id()),
                                "nickname": sender.get("nickname", "未知用户") if isinstance(sender, dict) else "未知用户"
                            },
                            "message": event.get_messages()
                        }
                        
                        nodes = await self.build_forward_nodes([msg_data], include_images=True)
                        
                        if nodes:
                            await event.bot.send_group_forward_msg(
                                group_id=monitor_group, messages=nodes
                            )
                    except Exception as e:
                        logger.warning(f"转发消息到群 {monitor_group} 失败: {e}")
        except Exception as e:
            logger.error(f"群消息处理异常: {e}")
