import asyncio
import json
import os
import tempfile
import random
from typing import Dict
from astrbot.api.all import * # 引入所有基础组件，包括 Plain
from astrbot.api.message_components import MessageChain # 显式导入 MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.star.star_tools import StarTools

# === 强制日志：使用 logger 而非 print，确保在控制台可见 ===
logger.warning("====== [防抖插件 v2.0] 模块正在加载！如果不出现此行，说明文件未更新！ ======")

@register(
    "astrbot_plugin_delay_ksc",
    "ks-c",
    "消息防抖 (拟人化随机版)",
    "2.0", # 版本升级：强制日志验证
)
class DebouncePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        
        logger.info("[防抖插件] v2.0 实例初始化成功。")
        
        DATA_DIR = StarTools.get_data_dir()
        self.CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

        default_wait = float(config.get("debounce_wait", 10))
        self.DEFAULT_CONFIG = {"enabled": True, "wait": default_wait, "jitter": 0.25}

        self.user_config: Dict[str, Dict[str, object]] = {}
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
            
            if state:
                # 核心逻辑：如果有任务，取消它（实现合并），并追加内容
                state["task"].cancel()
                state["prompts"].append(prompt)
                state["last_event"] = event 
                logger.info(f"[防抖插件] 检测到新消息，已合并。当前缓冲池大小: {len(state['prompts'])}")
            else:
                self.debounce_states[uid] = {
                    "prompts": [prompt], 
                    "task": None,
                    "last_event": event
                }

            async def debounce_closure():
                try:
                    mu = float(wait)
                    sigma = mu * jitter  
                    random_wait = random.gauss(mu, sigma)
                    final_wait = max(2.0, random_wait)
                    
                    logger.info(f"[防抖插件] (v2.0) 启动倒计时... (基准:{mu}s | 实际延迟:{final_wait:.2f}s)")
                    
                    await asyncio.sleep(final_wait)
                    
                    merged_prompt = ""
                    last_evt = None
                    
                    async with self._get_lock(uid):
                        current_state = self.debounce_states.get(uid)
                        if not current_state: return
                        
                        # 合并多条消息，用换行符分隔
                        merged_prompt = "\n".join(current_state["prompts"])
                        last_evt = current_state["last_event"]
                        self.debounce_states.pop(uid, None)
                    
                    # === 阶段1：调用 LLM ===
                    logger.info(f"[防抖插件] (v2.0) 倒计时结束，开始请求 LLM。合并内容: {merged_prompt[:50]}...")
                    provider = self.context.get_using_provider()
                    if not provider:
                        logger.error("[防抖插件] 错误：未找到可用的 Provider")
                        return

                    # 尝试调用 text_chat
                    response = await provider.text_chat(merged_prompt, session_id=uid)
                    
                    if not response or not response.completion_text:
                        logger.warning("[防抖插件] LLM 返回内容为空")
                        return

                    logger.info(f"[防抖插件] LLM 生成完毕，准备发送回复")

                    # === 阶段2：发送消息 ===
                    msg_chain = MessageChain([Plain(response.completion_text)])
                    await last_evt.send(msg_chain)

                except asyncio.CancelledError:
                    # 正常现象：倒计时被新消息打断
                    pass
                except Exception as e:
                    import traceback
                    logger.error(f"防抖回复过程出错: {e}")
                    logger.error(traceback.format_exc())

            task = asyncio.create_task(debounce_closure())
            self.debounce_states[uid]["task"] = task

    # 修改优先级为 0 (最高)，确保比系统默认处理先执行
    # 使用 *args 和 **kwargs 接收一切参数，彻底杜绝 TypeError
    @filter.on_llm_request(priority=0)
    async def on_llm_req(self, event: AstrMessageEvent, *args, **kwargs):
        
        req = None
        # 尝试从参数中提取 ProviderRequest
        for arg in args:
            if isinstance(arg, ProviderRequest):
                req = arg
                break
        
        # 如果 args 里没找到，可能 user 代码没更新，或者 AstrBot API 变了
        # 但大概率是 args[0] 就是 req
        if not req and args and isinstance(args[0], ProviderRequest):
            req = args[0]
            
        if not req:
            return

        umo = event.unified_msg_origin
        id = event.get_sender_id()
        name = event.get_sender_name()
        
        cfg = self.user_config.get(umo, self.DEFAULT_CONFIG)
        if not cfg["enabled"]:
            return

        # 构造带有用户身份的 Prompt
        if event.get_group_id() and req.prompt:
            req.prompt = f"[User ID: {id}, Nickname: {name}]\n{req.prompt.strip()}"

        current_jitter = cfg.get("jitter", 0.25)

        # 启动防抖任务
        await self.start_debounce_task(umo, req.prompt, wait=cfg["wait"], jitter=current_jitter, event=event)
        
        # 拦截事件：阻止 AstrBot 继续处理这条消息
        # 此时 priority=0，我们是第一个拿到的，只要 stop 成功，后面谁也拿不到
        event.stop_event()
