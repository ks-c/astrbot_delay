import asyncio

import json
import os
import tempfile
from typing import Dict
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_tools import StarTools


@register(
    "astrbot_plugin_chat_buffer",
    "ctrlkk",
    "消息防抖，指定时间内的多段消息合并处理",
    "1.0",
)
class DebouncePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        DATA_DIR = StarTools.get_data_dir()
        self.CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

        default_wait = float(config.get("debounce_wait", 10))
        self.DEFAULT_CONFIG = {"enabled": True, "wait": default_wait}

        # { uid: { "enabled": bool, "wait": float } }
        self.user_config: Dict[str, Dict[str, object]] = {}
        # 防抖状态 { uid: { "prompts": List[str], "task": asyncio.Task } }
        self.debounce_states: Dict[str, Dict] = {}
        # 每个 uid 独立的锁
        self.locks: Dict[str, asyncio.Lock] = {}

        self._load_config()

    async def initialize(self):
        pass

    async def terminate(self):
        pass

    def _load_config(self):
        if os.path.exists(self.CONFIG_FILE):
            try:
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    self.user_config = json.load(f)
            except Exception as e:
                logger.error(f"加载防抖配置失败: {e}")

    def _save_config(self):
        try:
            dirpath = os.path.dirname(self.CONFIG_FILE)
            fd, tmppath = tempfile.mkstemp(dir=dirpath, prefix="cfg-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.user_config, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmppath, self.CONFIG_FILE)
        except Exception as e:
            logger.error(f"保存防抖配置失败: {e}")

    @filter.command("开关防抖")
    async def toggle_debounce(self, event: AstrMessageEvent):
        """开关防抖"""
        uid = event.unified_msg_origin
        cfg = self.user_config.get(uid, self.DEFAULT_CONFIG)
        cfg["enabled"] = not cfg.get("enabled", False)
        self.user_config[uid] = cfg
        self._save_config()
        status = "开启" if cfg["enabled"] else "关闭"
        yield event.plain_result(f"防抖功能已{status}")

    @filter.command("设置防抖时间")
    async def set_debounce_time(self, event: AstrMessageEvent, wait: int):
        """设置防抖时间"""
        uid = event.unified_msg_origin
        if wait < 1:
            yield event.plain_result("防抖时间最少为1秒")
            return
        cfg = self.user_config.get(uid, self.DEFAULT_CONFIG)
        cfg["wait"] = wait
        self.user_config[uid] = cfg
        self._save_config()
        yield event.plain_result(f"防抖时间已设置为 {wait} 秒")

    def _get_lock(self, uid: str) -> asyncio.Lock:
        """获取/创建某个 uid 的独立锁"""
        if uid not in self.locks:
            self.locks[uid] = asyncio.Lock()
        return self.locks[uid]

    async def debounce_request(self, uid: str, prompt: str, wait: float) -> str:
        """异步防抖函数：同一 uid 的请求在 wait 秒内合并"""
        lock = self._get_lock(uid)
        async with lock:
            state = self.debounce_states.get(uid)
            if state:
                # 已有任务 -> 取消它，合并 prompt
                state["task"].cancel()
                state["prompts"].append(prompt)
                await asyncio.sleep(0)
            else:
                self.debounce_states[uid] = {"prompts": [prompt], "task": None}

            async def debounce_closure():
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    return None

                state = self.debounce_states.get(uid)
                if not state:
                    return None

                merged_prompt = "\n".join(state["prompts"])
                self.debounce_states.pop(uid, None)
                return merged_prompt

            task = asyncio.create_task(debounce_closure())
            self.debounce_states[uid]["task"] = task

        # 注意：等待必须放在锁外，否则别的请求要等整个 wait 才能进入
        result = await task
        return result

    # 设置为3，避免反复执行其它插件的耗时操作
    @filter.on_llm_request(priority=3)
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """请求开始"""
        umo = event.unified_msg_origin
        id = event.get_sender_id()
        name = event.get_sender_name()
        cfg = self.user_config.get(umo, self.DEFAULT_CONFIG)
        if not cfg["enabled"]:
            return

        # 群聊中加入用户识别
        if event.get_group_id() and req.prompt:
            req.prompt = f"[User ID: {id}, Nickname: {name}]\n{req.prompt.strip()}"

        merged_prompt = await self.debounce_request(umo, req.prompt, wait=cfg["wait"])
        if merged_prompt is None:
            event.stop_event()
            return
        req.prompt = merged_prompt
        logger.info(f"最终提示词：{req.prompt}")

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """请求结束"""
        uid = event.unified_msg_origin
        lock = self._get_lock(uid)
        async with lock:
            state = self.debounce_states.get(uid)
            if state and state["task"]:
                # 取消正在进行的防抖任务
                state["task"].cancel()
                try:
                    await state["task"]
                except asyncio.CancelledError:
                    pass  # 预期的取消错误
            self.debounce_states.pop(uid, None)
