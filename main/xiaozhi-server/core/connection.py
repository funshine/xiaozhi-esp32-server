import os
import copy
import json
import uuid
import time
import asyncio
import traceback

import websockets
from typing import Dict, Any
from plugins_func.loadplugins import auto_import_modules
from config.logger import setup_logging
from core.utils.dialogue import Message, Dialogue
from core.handle.textHandle import handleTextMessage
from core.utils.util import (
    get_string_no_punctuation_or_emoji,
    extract_json_from_string,
    get_ip_info,
    initialize_modules,
)
from core.handle.sendAudioHandle import sendAudioMessage
from core.handle.receiveAudioHandle import handleAudioMessage
from core.handle.functionHandler import FunctionHandler
from plugins_func.register import Action, ActionResponse
from core.auth import AuthMiddleware, AuthenticationError
from core.mcp.manager import MCPManager
from config.config_loader import (
    get_private_config_from_api,
    DeviceNotFoundException,
    DeviceBindException,
)

TAG = __name__

auto_import_modules("plugins_func.functions")


class TTSException(RuntimeError):
    pass


class ConnectionHandler:
    def __init__(
        self, config: Dict[str, Any], _vad, _asr, _llm, _tts, _memory, _intent
    ):
        self.config = copy.deepcopy(config)
        self.logger = setup_logging()
        self.auth = AuthMiddleware(config)

        self.need_bind = False
        self.bind_code = None

        self.websocket = None
        self.headers = None
        self.client_ip = None
        self.client_ip_info = {}
        self.session_id = None
        self.prompt = None
        self.welcome_msg = None

        # 客户端状态相关
        self.client_abort = False
        self.client_listen_mode = "auto"

        # 线程任务相关
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.tts_queue = asyncio.Queue()
        self.tts_files = {}
        self.tts_files_lock = asyncio.Lock()
        self.audio_play_queue = asyncio.Queue()
        self.background_tasks = set()

        self.tts_task_pause_event = asyncio.Event()
        self.tts_task_pause_event.set()
        self.audio_play_task_pause_event = asyncio.Event()
        self.audio_play_task_pause_event.set()

        # 依赖的组件
        self.vad = _vad
        self.asr = _asr
        self.llm = _llm
        self.tts = _tts
        self.memory = _memory
        self.intent = _intent

        # vad相关变量
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_have_voice_last_time = 0.0
        self.client_no_voice_last_time = 0.0
        self.client_voice_stop = False

        # asr相关变量
        self.asr_audio = []
        self.asr_server_receive = True

        # llm相关变量
        self.llm_finish_task = False
        self.dialogue = Dialogue()

        # tts相关变量
        self.tts_first_text_index = -1
        self.tts_last_text_index = -1

        # iot相关变量
        self.iot_descriptors = {}
        self.func_handler = None

        self.cmd_exit = self.config["exit_commands"]
        self.max_cmd_length = 0
        for cmd in self.cmd_exit:
            if len(cmd) > self.max_cmd_length:
                self.max_cmd_length = len(cmd)

        self.close_after_chat = False  # 是否在聊天结束后关闭连接
        self.use_function_call_mode = False

    async def handle_connection(self, ws):
        try:
            # 获取并验证headers
            self.headers = dict(ws.request.headers)

            if self.headers.get("device-id", None) is None:
                # 尝试从 URL 的查询参数中获取 device-id
                from urllib.parse import parse_qs, urlparse

                # 从 WebSocket 请求中获取路径
                request_path = ws.request.path
                if not request_path:
                    self.logger.bind(tag=TAG).error("无法获取请求路径")
                    return
                parsed_url = urlparse(request_path)
                query_params = parse_qs(parsed_url.query)
                if "device-id" in query_params:
                    self.headers["device-id"] = query_params["device-id"][0]
                    self.headers["client-id"] = query_params["client-id"][0]
                else:
                    self.logger.bind(tag=TAG).error(
                        "无法从请求头和URL查询参数中获取device-id"
                    )
                    return

            # 获取客户端ip地址
            self.client_ip = ws.remote_address[0]
            self.logger.bind(tag=TAG).info(
                f"{self.client_ip} conn - Headers: {self.headers}"
            )

            # 进行认证
            await self.auth.authenticate(self.headers)

            # 认证通过,继续处理
            self.websocket = ws
            self.session_id = str(uuid.uuid4())

            self.welcome_msg = copy.deepcopy(self.config["xiaozhi"])
            self.welcome_msg["session_id"] = self.session_id
            # await self.websocket.send(json.dumps(self.welcome_msg))

            # 异步初始化
            await self.loop.run_in_executor(None, self._initialize_components)

            # 启动TTS任务
            tts_task = asyncio.create_task(self._tts_priority_task())
            self.background_tasks.add(tts_task)
            tts_task.add_done_callback(self.background_tasks.discard)

            # 启动音频播放任务
            audio_play_task = asyncio.create_task(self._audio_play_priority_task())
            self.background_tasks.add(audio_play_task)
            audio_play_task.add_done_callback(self.background_tasks.discard)

            try:
                async for message in self.websocket:
                    await self._route_message(message)
            except websockets.exceptions.ConnectionClosed:
                self.logger.bind(tag=TAG).info("客户端断开连接")

        except AuthenticationError as e:
            self.logger.bind(tag=TAG).error(f"Authentication failed: {str(e)}")
            return
        except Exception as e:
            stack_trace = traceback.format_exc()
            self.logger.bind(tag=TAG).error(f"Connection error: {str(e)}-{stack_trace}")
            return
        finally:
            await self._save_and_close(ws)

    async def _save_and_close(self, ws):
        """保存记忆并关闭连接"""
        try:
            await self.memory.save_memory(self.dialogue.dialogue)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"保存记忆失败: {e}")
        finally:
            await self.close(ws)

    async def _route_message(self, message):
        """消息路由"""
        if isinstance(message, str):
            await handleTextMessage(self, message)
        elif isinstance(message, bytes):
            await handleAudioMessage(self, message)

    def _initialize_components(self):
        """初始化组件"""
        self._initialize_models()

        """加载提示词"""
        self.dialogue.put(Message(role="system", content=self.prompt))

        """加载记忆"""
        self._initialize_memory()
        """加载意图识别"""
        self._initialize_intent()
        """加载位置信息"""
        self.client_ip_info = get_ip_info(self.client_ip, self.logger)
        if self.client_ip_info is not None and "city" in self.client_ip_info:
            self.logger.bind(tag=TAG).info(f"Client ip info: {self.client_ip_info}")
            self.prompt = self.prompt + f"\nuser location:{self.client_ip_info}"

            self.dialogue.update_system_message(self.prompt)

    def _initialize_models(self):
        read_config_from_api = self.config.get("read_config_from_api", False)
        """如果是从配置文件获取，则进行二次实例化"""
        if not read_config_from_api:
            return
        """从接口获取差异化的配置进行二次实例化，非全量重新实例化"""
        try:
            private_config = get_private_config_from_api(
                self.config,
                self.headers.get("device-id", None),
                self.headers.get("client-id", None),
            )
            private_config["delete_audio"] = bool(self.config.get("delete_audio", True))
            self.logger.bind(tag=TAG).info(f"获取差异化配置成功: {private_config}")
        except DeviceNotFoundException as e:
            self.need_bind = True
            private_config = {}
        except DeviceBindException as e:
            self.need_bind = True
            self.bind_code = e.bind_code
            private_config = {}
        except Exception as e:
            self.need_bind = True
            self.logger.bind(tag=TAG).error(f"获取差异化配置失败: {e}")
            private_config = {}

        init_vad, init_asr, init_llm, init_tts, init_memory, init_intent = (
            False,
            False,
            False,
            False,
            False,
            False,
        )
        if private_config.get("VAD", None) is not None:
            init_vad = True
            self.config["VAD"] = private_config["VAD"]
            self.config["selected_module"]["VAD"] = private_config["selected_module"][
                "VAD"
            ]
        if private_config.get("ASR", None) is not None:
            init_asr = True
            self.config["ASR"] = private_config["ASR"]
            self.config["selected_module"]["ASR"] = private_config["selected_module"][
                "ASR"
            ]
        if private_config.get("LLM", None) is not None:
            init_llm = True
            self.config["LLM"] = private_config["LLM"]
            self.config["selected_module"]["LLM"] = private_config["selected_module"][
                "LLM"
            ]
        if private_config.get("TTS", None) is not None:
            init_tts = True
            self.config["TTS"] = private_config["TTS"]
            self.config["selected_module"]["TTS"] = private_config["selected_module"][
                "TTS"
            ]
        if private_config.get("Memory", None) is not None:
            init_memory = True
            self.config["Memory"] = private_config["Memory"]
            self.config["selected_module"]["Memory"] = private_config[
                "selected_module"
            ]["Memory"]
        if private_config.get("Intent", None) is not None:
            init_intent = True
            self.config["Intent"] = private_config["Intent"]
            self.config["selected_module"]["Intent"] = private_config[
                "selected_module"
            ]["Intent"]
        if private_config.get("Prompt", None) is not None:
            self.config["Prompt"] = private_config["Prompt"]
            self.config["selected_module"]["Prompt"] = private_config[
                "selected_module"
            ]["Prompt"]
        try:
            modules = initialize_modules(
                self.logger,
                private_config,
                init_vad,
                init_asr,
                init_llm,
                init_tts,
                init_memory,
                init_intent,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"初始化组件失败: {e}")
            modules = {}
        if modules.get("vad", None) is not None:
            self.vad = modules["vad"]
        if modules.get("asr", None) is not None:
            self.asr = modules["asr"]
        if modules.get("tts", None) is not None:
            self.tts = modules["tts"]
        if modules.get("llm", None) is not None:
            self.llm = modules["llm"]
        if modules.get("intent", None) is not None:
            self.intent = modules["intent"]
        if modules.get("memory", None) is not None:
            self.memory = modules["memory"]
        if modules.get("prompt", None) is not None:
            self.change_system_prompt(modules["prompt"])

    def _initialize_memory(self):
        """初始化记忆模块"""
        device_id = self.headers.get("device-id", None)
        self.memory.init_memory(device_id, self.llm)

    async def _initialize_intent(self):
        if (
            self.config["Intent"][self.config["selected_module"]["Intent"]]["type"]
            == "function_call"
        ):
            self.use_function_call_mode = True
        """初始化意图识别模块"""
        # 获取意图识别配置
        intent_config = self.config["Intent"]
        intent_type = self.config["Intent"][self.config["selected_module"]["Intent"]][
            "type"
        ]

        # 如果使用 nointent，直接返回
        if intent_type == "nointent":
            return
        # 使用 intent_llm 模式
        elif intent_type == "intent_llm":
            intent_llm_name = intent_config[self.config["selected_module"]["Intent"]][
                "llm"
            ]

            if intent_llm_name and intent_llm_name in self.config["LLM"]:
                # 如果配置了专用LLM，则创建独立的LLM实例
                from core.utils import llm as llm_utils

                intent_llm_config = self.config["LLM"][intent_llm_name]
                intent_llm_type = intent_llm_config.get("type", intent_llm_name)
                intent_llm = llm_utils.create_instance(
                    intent_llm_type, intent_llm_config
                )
                self.logger.bind(tag=TAG).info(
                    f"为意图识别创建了专用LLM: {intent_llm_name}, 类型: {intent_llm_type}"
                )
                self.intent.set_llm(intent_llm)
            else:
                # 否则使用主LLM
                self.intent.set_llm(self.llm)
                self.logger.bind(tag=TAG).info("使用主LLM作为意图识别模型")

        """加载插件"""
        self.func_handler = FunctionHandler(self)
        self.mcp_manager = MCPManager(self)

        """加载MCP工具"""
        await self.mcp_manager.initialize_servers()

    def change_system_prompt(self, prompt):
        self.prompt = prompt
        # 更新系统prompt至上下文
        self.dialogue.update_system_message(self.prompt)

    async def chat(self, query):

        self.dialogue.put(Message(role="user", content=query))

        response_message = []
        processed_chars = 0  # 跟踪已处理的字符位置
        try:
            # 使用带记忆的对话
            memory_str = await self.memory.query_memory(query)

            self.logger.bind(tag=TAG).debug(f"记忆内容: {memory_str}")
            llm_responses = self.llm.response(
                self.session_id, self.dialogue.get_llm_dialogue_with_memory(memory_str)
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            return None

        self.llm_finish_task = False
        text_index = 0
        for content in llm_responses:
            response_message.append(content)
            if self.client_abort:
                break

            # 合并当前全部文本并处理未分割部分
            full_text = "".join(response_message)
            current_text = full_text[processed_chars:]  # 从未处理的位置开始

            # 查找最后一个有效标点
            punctuations = ("。", "？", "！", "；", "：")
            last_punct_pos = -1
            for punct in punctuations:
                pos = current_text.rfind(punct)
                if pos > last_punct_pos:
                    last_punct_pos = pos

            # 找到分割点则处理
            if last_punct_pos != -1:
                segment_text_raw = current_text[: last_punct_pos + 1]
                segment_text = get_string_no_punctuation_or_emoji(segment_text_raw)
                if segment_text:
                    # 强制设置空字符，测试TTS出错返回语音的健壮性
                    # if text_index % 2 == 0:
                    #     segment_text = " "
                    text_index += 1
                    self.recode_first_last_text(segment_text, text_index)
                    await self.tts_queue.put((segment_text, text_index))
                    processed_chars += len(segment_text_raw)  # 更新已处理字符位置

        # 处理最后剩余的文本
        full_text = "".join(response_message)
        remaining_text = full_text[processed_chars:]
        if remaining_text:
            segment_text = get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                text_index += 1
                self.recode_first_last_text(segment_text, text_index)
                await self.tts_queue.put((segment_text, text_index))

        self.llm_finish_task = True
        self.dialogue.put(Message(role="assistant", content="".join(response_message)))
        self.logger.bind(tag=TAG).debug(
            json.dumps(self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False)
        )
        return True

    def create_chat_task(self, query):
        task = asyncio.create_task(self.chat(query))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def chat_with_function_calling(self, query, tool_call=False):
        self.logger.bind(tag=TAG).debug(f"Chat with function calling start: {query}")
        """Chat with function calling for intent detection using streaming"""

        if not tool_call:
            self.dialogue.put(Message(role="user", content=query))

        # Define intent functions
        functions = None
        if hasattr(self, "func_handler"):
            functions = self.func_handler.get_functions()
        response_message = []
        processed_chars = 0  # 跟踪已处理的字符位置

        try:
            start_time = time.time()

            # 使用带记忆的对话
            memory_str = await self.memory.query_memory(query)

            # self.logger.bind(tag=TAG).info(f"对话记录: {self.dialogue.get_llm_dialogue_with_memory(memory_str)}")

            # 使用支持functions的streaming接口
            llm_responses = self.llm.response_with_functions(
                self.session_id,
                self.dialogue.get_llm_dialogue_with_memory(memory_str),
                functions=functions,
            )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM 处理出错 {query}: {e}")
            return None

        self.llm_finish_task = False
        text_index = 0

        # 处理流式响应
        tool_call_flag = False
        function_name = None
        function_id = None
        function_arguments = ""
        content_arguments = ""
        for response in llm_responses:
            content, tools_call = response
            if "content" in response:
                content = response["content"]
                tools_call = None
            if content is not None and len(content) > 0:
                if len(response_message) <= 0 and (
                    content == "```" or "<tool_call>" in content
                ):
                    tool_call_flag = True

            if tools_call is not None:
                tool_call_flag = True
                if tools_call[0].id is not None:
                    function_id = tools_call[0].id
                if tools_call[0].function.name is not None:
                    function_name = tools_call[0].function.name
                if tools_call[0].function.arguments is not None:
                    function_arguments += tools_call[0].function.arguments

            if content is not None and len(content) > 0:
                if tool_call_flag:
                    content_arguments += content
                else:
                    response_message.append(content)

                    if self.client_abort:
                        break

                    end_time = time.time()
                    # self.logger.bind(tag=TAG).debug(f"大模型返回时间: {end_time - start_time} 秒, 生成token={content}")

                    # 处理文本分段和TTS逻辑
                    # 合并当前全部文本并处理未分割部分
                    full_text = "".join(response_message)
                    current_text = full_text[processed_chars:]  # 从未处理的位置开始

                    # 查找最后一个有效标点
                    punctuations = ("。", "？", "！", "；", "：")
                    last_punct_pos = -1
                    for punct in punctuations:
                        pos = current_text.rfind(punct)
                        if pos > last_punct_pos:
                            last_punct_pos = pos

                    # 找到分割点则处理
                    if last_punct_pos != -1:
                        segment_text_raw = current_text[: last_punct_pos + 1]
                        segment_text = get_string_no_punctuation_or_emoji(
                            segment_text_raw
                        )
                        if segment_text:
                            text_index += 1
                            self.recode_first_last_text(segment_text, text_index)
                            await self.tts_queue.put((segment_text, text_index))
                            # 更新已处理字符位置
                            processed_chars += len(segment_text_raw)

        # 处理function call
        if tool_call_flag:
            bHasError = False
            if function_id is None:
                a = extract_json_from_string(content_arguments)
                if a is not None:
                    try:
                        content_arguments_json = json.loads(a)
                        function_name = content_arguments_json["name"]
                        function_arguments = json.dumps(
                            content_arguments_json["arguments"], ensure_ascii=False
                        )
                        function_id = str(uuid.uuid4().hex)
                    except Exception as e:
                        bHasError = True
                        response_message.append(a)
                else:
                    bHasError = True
                    response_message.append(content_arguments)
                if bHasError:
                    self.logger.bind(tag=TAG).error(
                        f"function call error: {content_arguments}"
                    )
                else:
                    function_arguments = json.loads(function_arguments)
            if not bHasError:
                self.logger.bind(tag=TAG).info(
                    f"function_name={function_name}, function_id={function_id}, function_arguments={function_arguments}"
                )
                function_call_data = {
                    "name": function_name,
                    "id": function_id,
                    "arguments": function_arguments,
                }

                # 处理MCP工具调用
                if self.mcp_manager.is_mcp_tool(function_name):
                    result = await self._handle_mcp_tool_call(function_call_data)
                else:
                    # 处理系统函数
                    result = self.func_handler.handle_llm_function_call(
                        self, function_call_data
                    )
                await self._handle_function_result(result, function_call_data, text_index + 1)

        # 处理最后剩余的文本
        full_text = "".join(response_message)
        remaining_text = full_text[processed_chars:]
        if remaining_text:
            segment_text = get_string_no_punctuation_or_emoji(remaining_text)
            if segment_text:
                text_index += 1
                self.recode_first_last_text(segment_text, text_index)
                await self.tts_queue.put((segment_text, text_index))

        # 存储对话内容
        if len(response_message) > 0:
            self.dialogue.put(
                Message(role="assistant", content="".join(response_message))
            )

        self.llm_finish_task = True
        self.logger.bind(tag=TAG).debug(
            json.dumps(self.dialogue.get_llm_dialogue(), indent=4, ensure_ascii=False)
        )

        return True

    async def _handle_mcp_tool_call(self, function_call_data):
        function_arguments = function_call_data["arguments"]
        function_name = function_call_data["name"]
        try:
            args_dict = function_arguments
            if isinstance(function_arguments, str):
                try:
                    args_dict = json.loads(function_arguments)
                except json.JSONDecodeError:
                    self.logger.bind(tag=TAG).error(
                        f"无法解析 function_arguments: {function_arguments}"
                    )
                    return ActionResponse(
                        action=Action.REQLLM, result="参数解析失败", response=""
                    )

            tool_result = await self.mcp_manager.execute_tool(function_name, args_dict)
            # meta=None content=[TextContent(type='text', text='北京当前天气:\n温度: 21°C\n天气: 晴\n湿度: 6%\n风向: 西北 风\n风力等级: 5级', annotations=None)] isError=False
            content_text = ""
            if tool_result is not None and tool_result.content is not None:
                for content in tool_result.content:
                    content_type = content.type
                    if content_type == "text":
                        content_text = content.text
                    elif content_type == "image":
                        pass

            if len(content_text) > 0:
                return ActionResponse(
                    action=Action.REQLLM, result=content_text, response=""
                )

        except Exception as e:
            self.logger.bind(tag=TAG).error(f"MCP工具调用错误: {e}")
            return ActionResponse(
                action=Action.REQLLM, result="工具调用出错", response=""
            )

        return ActionResponse(action=Action.REQLLM, result="工具调用出错", response="")

    def create_chat_with_function_calling_task(self, query, tool_call=False):
        task = asyncio.create_task(self.chat_with_function_calling(query, tool_call))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def _handle_function_result(self, result, function_call_data, text_index):
        if result.action == Action.RESPONSE:  # 直接回复前端
            text = result.response
            self.recode_first_last_text(text, text_index)
            await self.tts_queue.put((text, text_index))
            self.dialogue.put(Message(role="assistant", content=text))
        elif result.action == Action.REQLLM:  # 调用函数后再请求llm生成回复

            text = result.result
            if text is not None and len(text) > 0:
                function_id = function_call_data["id"]
                function_name = function_call_data["name"]
                function_arguments = function_call_data["arguments"]
                self.dialogue.put(
                    Message(
                        role="assistant",
                        tool_calls=[
                            {
                                "id": function_id,
                                "function": {
                                    "arguments": function_arguments,
                                    "name": function_name,
                                },
                                "type": "function",
                                "index": 0,
                            }
                        ],
                    )
                )

                self.dialogue.put(
                    Message(role="tool", tool_call_id=function_id, content=text)
                )
                await self.chat_with_function_calling(text, tool_call=True)
        elif result.action == Action.NOTFOUND:
            text = result.result
            self.recode_first_last_text(text, text_index)
            self.tts_queue.put((text, text_index))
            self.dialogue.put(Message(role="assistant", content=text))
        else:
            text = result.result
            self.recode_first_last_text(text, text_index)
            self.tts_queue.put((text, text_index))
            self.dialogue.put(Message(role="assistant", content=text))

    async def _tts_priority_task(self):
        while not self.stop_event.is_set():
            await self.tts_task_pause_event.wait()  # 等待事件被 set，未 set 时阻塞
            text = None
            try:
                text, text_index = await self.tts_queue.get()
                if text is None or len(text) <= 0:
                    if self.stop_event.is_set():
                        break
                    continue
                opus_datas, tts_file = [], None
                try:
                    self.logger.bind(tag=TAG).debug("正在处理TTS任务...")
                    tts_timeout = int(self.config.get("tts_timeout", 10))
                    tts_file, text, text_index = await asyncio.wait_for(self.speak_and_play(text, text_index), timeout=tts_timeout)
                    if text is None or len(text) <= 0:
                        self.logger.bind(tag=TAG).error(
                            f"TTS出错：{text_index}: tts text is empty"
                        )
                    elif tts_file is None:
                        self.logger.bind(tag=TAG).error(
                            f"TTS出错： file is empty: {text_index}: {text}"
                        )
                    else:
                        self.logger.bind(tag=TAG).debug(
                            f"TTS生成：文件路径: {tts_file}"
                        )
                        if os.path.exists(tts_file):
                            opus_datas, duration = self.tts.audio_to_opus_data(tts_file)
                        else:
                            self.logger.bind(tag=TAG).error(
                                f"TTS出错：文件不存在{tts_file}"
                            )
                except asyncio.TimeoutError:
                    self.logger.bind(tag=TAG).error("TTS超时")
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"TTS出错: {e}")
                if not self.client_abort:
                    # 如果没有中途打断就发送语音
                    await self.audio_play_queue.put((opus_datas, text, text_index))
                if (
                    self.tts.delete_audio_file
                    and tts_file is not None
                    and os.path.exists(tts_file)
                ):
                    os.remove(tts_file)
                    # 从字典中删除文件路径
                    async with self.tts_files_lock:
                        if tts_file in self.tts_files:
                            del self.tts_files[tts_file]
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"TTS任务处理错误: {e}")
                self.clearSpeakStatus()
                await self.websocket.send(
                    json.dumps(
                        {
                            "type": "tts",
                            "state": "stop",
                            "session_id": self.session_id,
                        }
                    )
                )
                self.logger.bind(tag=TAG).error(
                    f"tts_priority task error: {text} {e}"
                )

    async def _audio_play_priority_task(self):
        while not self.stop_event.is_set():
            await self.audio_play_task_pause_event.wait()  # 等待事件被 set，未 set 时阻塞
            text = None
            try:
                if self.stop_event.is_set():
                    break
                opus_datas, text, text_index = await self.audio_play_queue.get()
                await sendAudioMessage(self, opus_datas, text, text_index)
            except Exception as e:
                self.logger.bind(tag=TAG).error(
                    f"audio_play_priority task error: {text} {e}"
                )

    async def speak_and_play(self, text, text_index=0):
        if text is None or len(text) <= 0:
            self.logger.bind(tag=TAG).info(f"无需tts转换，query为空，{text}")
            return None, text, text_index
        tts_file = await self.loop.run_in_executor(None, self.tts.to_tts, text)
        if tts_file is None:
            self.logger.bind(tag=TAG).error(f"tts转换失败，{text}")
            return None, text, text_index
        self.logger.bind(tag=TAG).debug(f"TTS 文件生成完毕: {tts_file}")
        async with self.tts_files_lock:
            self.tts_files[tts_file] = True
        return tts_file, text, text_index

    def clearSpeakStatus(self):
        self.logger.bind(tag=TAG).debug(f"清除服务端讲话状态")
        self.asr_server_receive = True
        self.tts_last_text_index = -1
        self.tts_first_text_index = -1

    def recode_first_last_text(self, text, text_index=0):
        if self.tts_first_text_index == -1:
            self.logger.bind(tag=TAG).info(f"大模型说出第一句话: {text}")
            self.tts_first_text_index = text_index
        self.tts_last_text_index = text_index

    async def close(self, ws=None):
        """资源清理方法"""
        # 清理MCP资源
        if hasattr(self, "mcp_manager") and self.mcp_manager:
            await self.mcp_manager.cleanup_all()

        # 触发停止事件并清理资源
        if self.stop_event:
            self.stop_event.set()

        # 等待所有后台任务退出
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
            self.background_tasks.clear()

        # 清空任务队列
        await self._clear_queues()

        if ws:
            await ws.close()
        elif self.websocket:
            await self.websocket.close()
        self.logger.bind(tag=TAG).info("连接资源已释放")

    async def _clear_queue(self, queue: asyncio.Queue):
        # 清空单个任务队列
        if not queue:
            return
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _clear_queues(self):
        # 清空所有任务队列
        await self._clear_queue(self.tts_queue)
        await self._clear_queue(self.audio_play_queue)

    def reset_vad_states(self):
        self.client_audio_buffer = bytearray()
        self.client_have_voice = False
        self.client_have_voice_last_time = 0
        self.client_voice_stop = False
        self.logger.bind(tag=TAG).debug("VAD states reset.")

    async def chat_and_close(self, text):
        """Chat with the user and then close the connection"""
        try:
            # Use the existing chat method
            await self.chat(text)

            # After chat is complete, close the connection
            self.close_after_chat = True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Chat and close error: {str(e)}")

    def create_chat_and_close_task(self, text):
        task = asyncio.create_task(self.chat_and_close(text))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def abort_last_chat(self):
        """Abort the current chat"""
        self.logger.bind(tag=TAG).debug("Aborting chat.")
        self.client_abort = True
        self.tts_task_pause_event.clear()  # 暂停TTS线程
        self.audio_play_task_pause_event.clear()  # 暂停音频播放线程

        try:
            await asyncio.sleep(0.1)
            # 打断客户端说话状态
            await self.websocket.send(json.dumps({"type": "tts", "state": "stop", "session_id": self.session_id}))
            self.logger.bind(tag=TAG).debug("Client speaking status cleared.")
            self.clearSpeakStatus()  # 清除服务端讲话状态
            self.logger.bind(tag=TAG).debug("Cleared server speaking status.")
            await self._clear_queues()  # 清空任务队列
            self.logger.bind(tag=TAG).debug("Cleared task queues.")
            self.logger.bind(tag=TAG).debug("Chat aborted.")

            # 删除tts文件
            if self.tts.delete_audio_file:
                for tts_file in list(self.tts_files.keys()):
                    if tts_file is not None:
                        async with self.tts_files_lock:
                            try:
                                # 先检查再删除，保证原子性
                                if os.path.exists(tts_file):
                                    os.remove(tts_file)
                                del self.tts_files[tts_file]
                            except (OSError, KeyError) as e:
                                self.logger.warning(f"Failed to clean up file {tts_file}: {str(e)}")

        finally:
            self.client_abort = False
            self.audio_play_task_pause_event.set()  # 恢复播放线程
            self.tts_task_pause_event.set()  # 恢复TTS线程
