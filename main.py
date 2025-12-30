import asyncio
import json
import os
import tempfile
import random
from typing import Dict
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_tools import StarTools

@register(
    "astrbot_delay_ksc",
    "ks-c",
    "消息防抖 (拟人化随机版)",
    "1.3",
)
class DebouncePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        DATA_DIR = StarTools.get_data_dir()
        self.CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

        default_wait = float(config.get("debounce_wait", 10))
        self.DEFAULT_CONFIG = {"enabled": True, "wait": default_wait, "jitter": 0.25}

        self.user_config: Dict[str, Dict[str, object]] = {}
        # 结构修改：增加 last_event 用于主动回复
        self.debounce_states: Dict[str, Dict] = {}
        self.locks: Dict[str, asyncio.Lock] = {}

        self._load_config()

    async def initialize(self): pass
    async def terminate(self): pass
    
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
        uid = event.unified_msg_origin
        cfg = self.user_config.get(uid, self.DEFAULT_CONFIG)
        cfg["enabled"] = not cfg.get("enabled", False)
        self.user_config[uid] = cfg
        self._save_config()
        status = "开启" if cfg["enabled"] else "关闭"
        yield event.plain_result(f"防抖功能已{status}")

    @filter.command("设置防抖时间")
    async def set_debounce_time(self, event: AstrMessageEvent, wait: int):
        uid = event.unified_msg_origin
        if wait < 1:
            yield event.plain_result("防抖时间最少为1秒")
            return
        cfg = self.user_config.get(uid, self.DEFAULT_CONFIG)
        cfg["wait"] = wait
        self.user_config[uid] = cfg
        self._save_config()
        yield event.plain_result(f"防抖等待时间已设置为 {wait} 秒")

    @filter.command("设置波动")
    async def set_jitter(self, event: AstrMessageEvent, jitter: float):
        uid = event.unified_msg_origin
        if jitter < 0 or jitter > 1.0:
            yield event.plain_result("波动系数建议在 0.0 到 1.0 之间")
            return
        cfg = self.user_config.get(uid, self.DEFAULT_CONFIG)
        cfg["jitter"] = jitter
        self.user_config[uid] = cfg
        self._save_config()
        yield event.plain_result(f"回复随机波动系数已设置为: {jitter}")

    def _get_lock(self, uid: str) -> asyncio.Lock:
        if uid not in self.locks:
            self.locks[uid] = asyncio.Lock()
        return self.locks[uid]

    async def start_debounce_task(self, uid: str, prompt: str, wait: float, jitter: float, event: AstrMessageEvent):
        lock = self._get_lock(uid)
        async with lock:
            state = self.debounce_states.get(uid)
            
            # 1. 如果有正在等待的任务，取消它（重置倒计时）
            if state:
                state["task"].cancel()
                state["prompts"].append(prompt)
                state["last_event"] = event 
            else:
                # 2. 如果没有，创建新状态
                self.debounce_states[uid] = {
                    "prompts": [prompt], 
                    "task": None,
                    "last_event": event
                }

            # 3. 定义后台执行的闭包函数
            async def debounce_closure():
                try:
                    # 计算随机延迟
                    mu = float(wait)
                    sigma = mu * jitter  
                    random_wait = random.gauss(mu, sigma)
                    final_wait = max(2.0, random_wait)
                    
                    logger.info(f"[防抖插件] 正在等待... (基准:{mu}s | 实际延迟:{final_wait:.2f}s)")
                    
                    # 在这里等待，不阻塞主线程
                    await asyncio.sleep(final_wait)
                    
                    # === 等待结束，开始回复 ===
                    merged_prompt = ""
                    last_evt = None
                    
                    async with self._get_lock(uid):
                        current_state = self.debounce_states.get(uid)
                        if not current_state: return
                        
                        merged_prompt = "\n".join(current_state["prompts"])
                        last_evt = current_state["last_event"]
                        self.debounce_states.pop(uid, None)
                    
                    # === 主动调用 LLM 发送回复 ===
                    logger.info(f"[防抖插件] 触发回复，合并内容长度: {len(merged_prompt)}")
                    
                    provider = self.context.provider_manager.get_default_provider()
                    if provider:
                        # 调用 LLM 生成回复
                        response = await provider.text_chat(merged_prompt, session_id=uid)
                        if response:
                            await last_evt.send(response.completion_text)

                except asyncio.CancelledError:
                    # 任务被取消说明有新消息来了
                    pass
                except Exception as e:
                    logger.error(f"防抖回复过程出错: {e}")

            # 4. 启动任务
            task = asyncio.create_task(debounce_closure())
            self.debounce_states[uid]["task"] = task

    @filter.on_llm_request(priority=3)
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        """请求开始"""
        umo = event.unified_msg_origin
        id = event.get_sender_id()
        name = event.get_sender_name()
        
        cfg = self.user_config.get(umo, self.DEFAULT_CONFIG)
        if not cfg["enabled"]:
            return

        # 群聊信息补充
        if event.get_group_id() and req.prompt:
            req.prompt = f"[User ID: {id}, Nickname: {name}]\n{req.prompt.strip()}"

        current_jitter = cfg.get("jitter", 0.25)

        # 1. 启动/重置倒计时任务
        await self.start_debounce_task(umo, req.prompt, wait=cfg["wait"], jitter=current_jitter, event=event)
        
        # 2. 拦截当前事件，防止 AstrBot 立即处理
        event.stop_event()
