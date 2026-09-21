"""学习辅助猫娘 v0.1（作者：MENGYAOYUE）

一个会规划、会出题、会看卷子、还会做心理疏导的学习插件：

1. 规划     —— 按目标考试、目标分、剩余天数与每天可用时间生成长短期计划；
2. 联网     —— 连接本人学习平台账号同步真实进度，并检索免费公开课程资源；
3. 教学     —— 三段式（定义题 → 中等易错题 → 综合创新题），题型严格遵循命题规律；
4. 诊断     —— 从题目/试卷/地区/学校推断薄弱点，给出保守 / 最可能 / 理想三档分数预期；
5. 疏导     —— 按考试类型、临考阶段与情绪状态做个性化心理暗示与疏导。

设计约束：
- 零第三方依赖，网络用标准库 urllib；所有同步 IO 都经 ``asyncio.to_thread``；
- 平台连接只读取本人账号数据，遇到验证码交人工输入，不逆向加密参数；
- 凭据与学习数据只落在插件自己的 data/ 目录，绝不进 git。
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional


def _dump_crash(stage: str) -> None:
    """把崩溃现场写到临时目录。

    宿主经常直接吞掉插件子进程的 stderr（日志里只剩一句 start_plugin failed），
    没有 traceback 就没法定位。这里主动落盘一份，排查完即可删掉本函数。
    """
    import traceback

    try:
        target = Path(tempfile.gettempdir()) / f"neko_study_copilot_crash_{stage}.txt"
        target.write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass


_PROBE_MODULES = (
    "urllib.robotparser",
    "http.cookiejar",
    "http.server",
    "http.client",
    "html",
    "html.parser",
    "gzip",
    "zlib",
    "concurrent.futures",
    "sqlite3",
    "socket",
    "ssl",
    "base64",
    "binascii",
    "hashlib",
    "tempfile",
    "traceback",
    "dataclasses",
    "contextlib",
    "importlib.util",
    "statistics",
    "random",
    "difflib",
    "textwrap",
    "zoneinfo",
    "secrets",
    "uuid",
    "logging",
    "argparse",
)


def _probe_host_modules() -> None:
    """记录宿主冻结环境里可用的标准库模块。

    宿主是 PyInstaller 冻结进程：标准库里只有被它自己（或依赖）引用过的模块
    才会被打包，所以"标准库一定有"这个假设在这里不成立——``urllib.robotparser``
    就是活生生的例子。这份清单写进临时目录，排查插件启动问题时先看它，
    能省掉一轮"改一行、重启一次"的盲试。
    """
    import importlib.util

    try:
        lines = []
        for name in _PROBE_MODULES:
            try:
                ok = importlib.util.find_spec(name) is not None
            except Exception:
                ok = False
            lines.append(f"{'OK     ' if ok else 'MISSING'} {name}")
        target = Path(tempfile.gettempdir()) / "neko_study_copilot_host_modules.txt"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


_probe_host_modules()

try:
    from plugin.sdk.plugin import (
        Err,
        NekoPluginBase,
        Ok,
        SdkError,
        lifecycle,
        llm_tool,
        message,
        neko_plugin,
        plugin_entry,
    )

    from ._connectors import ConnectorManager
    from ._crawl import Crawler, extract_json_object
    from ._diagnose import build_forecast, diagnose, format_diagnosis, format_forecast
    from ._panel import AsyncBridge, PanelServer, find_open_port
    from ._planner import build_plan, format_plan, resolve_exam_date, subject_label
    from ._profiles import (
        format_profile,
        get_profile,
        list_exam_types,
        resolve_exam_type,
        subject_keys,
    )
    from ._psych import build_comfort_prompt, counsel, format_counsel
    from ._sources import ResourceSearcher, build_query, format_resources, list_sources
    from ._store import StudyStore
    from ._syllabus import find_point, format_point, get_point, match_points, points_for, roi_ranking
    from ._tutor import (
        TeachContext,
        build_grade_prompt,
        build_quiz_prompt,
        build_teach_prompt,
        decide_stage,
        format_questions,
        quiz_intro,
        validate_question_plan,
    )
except Exception:  # pragma: no cover - 只在宿主环境里用于定位
    _dump_crash("import")
    raise

_PLUGIN_ID = "neko_study_copilot"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def _safe_str(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _parse_json_payload(text: str) -> Any:
    """从大模型输出里抠出 JSON（对象或数组都支持）。"""
    if not text:
        return None
    candidates = [match.group(1) for match in _JSON_BLOCK_RE.finditer(text)]
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        for opener, closer in (("[", "]"), ("{", "}")):
            start = candidate.find(opener)
            end = candidate.rfind(closer)
            if start >= 0 and end > start:
                try:
                    return json.loads(candidate[start : end + 1])
                except Exception:
                    continue
    return None


def _extract_questions(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("questions", "items", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


@neko_plugin
class StudyCopilotPlugin(NekoPluginBase):
    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        self.data_dir = Path(self.data_path())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.static_dir = Path(__file__).resolve().parent / "static"

        # ── 配置（异步 startup 中由 config.dump() 注入）────────────
        self.exam_type: str = "gaokao"
        self.region: str = ""
        self.school: str = ""
        self.grade: str = ""
        self.subjects: str = ""
        self.target_score: float = 0.0
        self.exam_date: str = ""
        self.credits: str = ""       # 学分配置，形如 "math_adv:5,english:3"
        self.daily_minutes: int = 180
        self.teach_style: str = "gentle"
        self.quiz_stage: str = "auto"
        self.quiz_count: int = 3
        self.quiz_with_traps: bool = True
        self.network_enabled: bool = True
        self.free_source_search: bool = True
        self.platform_sync_enabled: bool = True
        self.crawl_interval_ms: int = 1500
        self.crawl_respect_robots: bool = True
        self.crawl_max_pages: int = 6
        self.crawl_timeout: float = 15.0
        self.psychology_enabled: bool = True
        self.psychology_level: str = "normal"
        self.vision_enabled: bool = True
        # 最近一条用户聊天消息里捕获的文本与图片（识图诊断用）
        self.last_capture: dict[str, Any] = {"text": "", "images": [], "ts": 0.0}
        self.admin_password: str = ""
        self.panel_port: int = 15880
        self.llm_timeout: float = 60.0
        self.request_timeout: float = 20.0
        self.catgirl_name: str = "猫娘"
        self._config_loaded: bool = False

        # ── 运行时组件 ────────────────────────────────────────
        self.store = StudyStore(self.data_dir / "study.db")
        self.crawler: Optional[Crawler] = None
        self.connectors: Optional[ConnectorManager] = None
        self.searcher: Optional[ResourceSearcher] = None
        self._panel_server: Optional[PanelServer] = None
        self._bridge = AsyncBridge()
        try:
            self._bridge.set_notifier(lambda msg: self.logger.warning("%s", msg))
        except Exception:
            pass
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── 配置 ──────────────────────────────────────────────────
    async def _load_config(self) -> None:
        try:
            cfg = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("[study_copilot] 读取配置失败，使用默认值: %s", exc)
            return
        section = cfg.get(_PLUGIN_ID)
        if not isinstance(section, dict):
            section = {}
        self.exam_type = resolve_exam_type(_safe_str(section.get("exam_type"), self.exam_type))
        self.region = _safe_str(section.get("region"), self.region)
        self.school = _safe_str(section.get("school"), self.school)
        self.grade = _safe_str(section.get("grade"), self.grade)
        self.subjects = _safe_str(section.get("subjects"), self.subjects)
        self.target_score = _safe_float(section.get("target_score"), self.target_score)
        self.exam_date = _safe_str(section.get("exam_date"), self.exam_date)
        self.credits = _safe_str(section.get("credits"), self.credits)
        self.daily_minutes = _safe_int(section.get("daily_minutes"), self.daily_minutes)
        self.teach_style = _safe_str(section.get("teach_style"), self.teach_style)
        self.quiz_stage = _safe_str(section.get("quiz_stage"), self.quiz_stage)
        self.quiz_count = _safe_int(section.get("quiz_count"), self.quiz_count)
        self.quiz_with_traps = _safe_bool(section.get("quiz_with_traps"), self.quiz_with_traps)
        self.network_enabled = _safe_bool(section.get("network_enabled"), self.network_enabled)
        self.free_source_search = _safe_bool(section.get("free_source_search"), self.free_source_search)
        self.platform_sync_enabled = _safe_bool(section.get("platform_sync_enabled"), self.platform_sync_enabled)
        self.crawl_interval_ms = _safe_int(section.get("crawl_interval_ms"), self.crawl_interval_ms)
        self.crawl_respect_robots = _safe_bool(section.get("crawl_respect_robots"), self.crawl_respect_robots)
        self.crawl_max_pages = _safe_int(section.get("crawl_max_pages"), self.crawl_max_pages)
        self.crawl_timeout = _safe_float(section.get("crawl_timeout"), self.crawl_timeout)
        self.psychology_enabled = _safe_bool(section.get("psychology_enabled"), self.psychology_enabled)
        self.psychology_level = _safe_str(section.get("psychology_level"), self.psychology_level)
        self.vision_enabled = _safe_bool(section.get("vision_enabled"), self.vision_enabled)
        self.admin_password = _safe_str(section.get("admin_password"), self.admin_password)
        self.panel_port = _safe_int(section.get("panel_port"), self.panel_port)
        self.llm_timeout = _safe_float(section.get("llm_timeout"), self.llm_timeout)
        self.request_timeout = _safe_float(section.get("request_timeout"), self.request_timeout)
        self.catgirl_name = _safe_str(section.get("catgirl_name"), self.catgirl_name)
        self._config_loaded = True

    async def _ensure_ready(self) -> None:
        if not self._config_loaded:
            await self._load_config()
        # 宿主可能在不同事件循环里调度 entry；只要原来那条已死就跟着当前循环走
        try:
            self._bridge.bind_if_dead(asyncio.get_running_loop())
        except RuntimeError:
            pass

    def _effective_profile(self) -> dict[str, Any]:
        """配置与数据库档案合并：数据库里填过的优先。"""
        stored = self.store.get_profile()
        profile = {
            "exam_type": self.exam_type,
            "region": self.region,
            "school": self.school,
            "grade": self.grade,
            "subjects": self.subjects,
            "target_score": self.target_score,
            "exam_date": self.exam_date,
            "daily_minutes": self.daily_minutes,
            "teach_style": self.teach_style,
            "quiz_stage": self.quiz_stage,
            "psychology_level": self.psychology_level,
            "credits": self.credits,
        }
        for key, value in stored.items():
            if value not in (None, "", 0):
                profile[key] = value
        if not profile.get("subjects"):
            profile["subjects"] = ",".join(subject_keys(profile["exam_type"], ""))
        return profile

    def _subjects_list(self, profile: dict[str, Any]) -> list[str]:
        raw = str(profile.get("subjects") or "")
        return [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]

    def _days_left(self, profile: dict[str, Any]) -> int:
        _date, days = resolve_exam_date(str(profile.get("exam_date") or ""), profile["exam_type"])
        return days

    # ── 大模型 ────────────────────────────────────────────────
    def _model_config(self, kind: str = "conversation") -> dict[str, Any]:
        try:
            from utils.config_manager import get_config_manager

            cfg = get_config_manager().get_model_api_config(kind)
            return cfg if isinstance(cfg, dict) else {}
        except Exception as exc:
            self.logger.warning("[study_copilot] 读取模型配置失败: %s", exc)
            return {}

    def _vision_model_config(self) -> dict[str, Any]:
        """视觉模型配置：优先宿主的 vision 档，没有就退回 conversation 档。"""
        cfg = self._model_config("vision")
        if _safe_str(cfg.get("model")):
            return cfg
        return self._model_config("conversation")

    async def _llm_chat(self, system: str, user: str, timeout: Optional[float] = None) -> str:
        """调用宿主配置的大模型。优先走官方 llm_client，失败降级到直连。"""
        cfg = self._model_config()
        model = _safe_str(cfg.get("model"))
        base_url = _safe_str(cfg.get("base_url")).rstrip("/")
        api_key = _safe_str(cfg.get("api_key"))
        if not (model and base_url and api_key):
            raise SdkError("尚未配置可用的会话模型，请先在 N.E.K.O 的模型设置里配置。")
        limit = float(timeout or self.llm_timeout)
        provider_type = _safe_str(cfg.get("provider_type")) or None

        def _build(**extra: Any) -> Any:
            from utils.llm_client import create_chat_llm_async

            kwargs: dict[str, Any] = {
                "model": model,
                "base_url": base_url,
                "api_key": api_key,
                "max_completion_tokens": 2048,
                "timeout": limit,
            }
            kwargs.update(extra)
            return create_chat_llm_async(**kwargs)

        try:
            llm = _build()
        except TypeError:
            llm = _build(provider_type=provider_type) if provider_type else _build()
        except ImportError:
            llm = None
        if llm is not None:
            try:
                result = await asyncio.wait_for(
                    llm.ainvoke(
                        [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ]
                    ),
                    timeout=limit,
                )
                text = getattr(result, "content", None) or _safe_str(result)
                if text:
                    return text
            except asyncio.TimeoutError:
                raise SdkError(f"模型响应超时（{int(limit)} 秒），请稍后再试。")
            except Exception as exc:
                self.logger.warning("[study_copilot] llm_client 调用失败，降级直连: %s", exc)
        return await asyncio.to_thread(self._llm_http, base_url, api_key, model, system, user, limit)

    def _llm_http(
        self,
        base_url: str,
        api_key: str,
        model: str,
        system: str,
        user: str,
        timeout: float,
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._llm_http_messages(base_url, api_key, model, messages, timeout)

    def _llm_http_messages(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        timeout: float,
    ) -> str:
        import urllib.error
        import urllib.request

        endpoint = f"{base_url}/chat/completions"
        body = json.dumps(
            {
                "model": model,
                "messages": messages,
                "max_completion_tokens": 2048,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise SdkError(f"模型请求失败：HTTP {exc.code}")
        except Exception as exc:
            raise SdkError(f"模型请求失败：{exc}")
        choices = payload.get("choices") or []
        if not choices:
            raise SdkError("模型没有返回内容。")
        message = choices[0].get("message") or {}
        return _safe_str(message.get("content")).strip()

    async def _llm_json(self, system: str, user: str, timeout: Optional[float] = None) -> Any:
        text = await self._llm_chat(system, user, timeout=timeout)
        payload = _parse_json_payload(text)
        if payload is None:
            payload = extract_json_object(text) or None
        if payload is None:
            raise SdkError("模型返回的内容不是合法 JSON，请重试或换个说法。")
        return payload

    # ── 识图（聊天框发截图 → 转写题目 → 诊断/讲解）────────────
    def _capture_chat(self, payload: Any) -> None:
        """记录用户最近一条聊天消息里的文本与图片，供识图诊断使用。

        宿主的消息结构可能随版本变化，这里做防御式解析：能认出 text/image/image_url
        三类部件即可，认不出就原样存一份文本，绝不抛异常影响宿主。
        """
        try:
            data = payload if isinstance(payload, dict) else {}
            if isinstance(payload, str) and payload.strip():
                data = {"text": payload.strip()}
            for key in ("message", "event", "data", "payload", "record"):
                inner = data.get(key)
                if isinstance(inner, dict):
                    merged = dict(inner)
                    merged.update({k: v for k, v in data.items() if k not in merged})
                    data = merged
                    break
            parts = data.get("parts") or data.get("attachments") or data.get("content") or []
            if isinstance(parts, dict):
                parts = [parts]
            texts: list[str] = []
            images: list[dict[str, Any]] = []
            for part in parts:
                if not isinstance(part, dict):
                    if isinstance(part, str) and part.strip():
                        texts.append(part.strip())
                    continue
                kind = _safe_str(part.get("type")).lower()
                if kind in ("", "text"):
                    text = _safe_str(part.get("text") or part.get("content")).strip()
                    if text:
                        texts.append(text)
                elif kind in ("image", "image_url"):
                    item: dict[str, Any] = {"mime": _safe_str(part.get("mime")) or "image/png"}
                    if isinstance(part.get("data"), (bytes, bytearray)):
                        item["data"] = bytes(part["data"])
                    for locator in ("path", "file", "url"):
                        value = _safe_str(part.get(locator)).strip()
                        if value:
                            item[locator] = value
                            break
                    if ("data" in item) or ("path" in item) or ("url" in item):
                        images.append(item)
            if not texts and not images:
                text = _safe_str(data.get("text") or data.get("content")).strip()
                if text:
                    texts.append(text)
            if texts or images:
                self.last_capture = {
                    "text": "\n".join(texts)[:4000],
                    "images": images[-4:],
                    "ts": time.time(),
                }
        except Exception as exc:
            self.logger.warning("[study_copilot] 聊天捕获失败（忽略）: %s", exc)

    @message(id="study_capture", source="chat")
    def on_chat_message(self, *args, **kwargs):
        """被动监听聊天：记住用户最近发的文本与截图，识图诊断时直接用。"""
        payload: Any = kwargs.get("payload") or kwargs
        if not isinstance(payload, dict) or not payload:
            payload = args[0] if args else {}
        self._capture_chat(payload)
        return Ok({"status": "captured"})

    def _capture_image_count(self) -> int:
        return len(self.last_capture.get("images") or [])

    async def _vision_transcribe(self, question: str) -> str:
        """把用户最近发的截图交给视觉模型转写成文字，并附上用户的提问。"""
        images = list(self.last_capture.get("images") or [])
        if not images:
            raise SdkError(
                "我还没收到你的截图喵。直接在聊天框把题目/试卷截图发给本喵，"
                "再说一句『识图诊断』或『讲讲这道题』就行，不用打字复制。"
            )
        item = images[-1]
        data_url = await asyncio.to_thread(self._image_to_data_url, item)
        prompt = (
            "你是学习辅助助手。这是用户发来的截图（可能是题目、试卷、作业、笔记或成绩单）。\n"
            "请完整转写图中的文字内容（保留题号、选项、分值、图注），"
            "若是试卷/成绩单请保留科目与分数；无法辨认的地方用〔无法辨认〕标注。\n"
            f"用户想问：{question or '（未说明，请按‘帮我看看这些题’处理）'}\n"
            "只输出转写文本，不要点评。"
        )
        cfg = self._vision_model_config()
        model = _safe_str(cfg.get("model"))
        base_url = _safe_str(cfg.get("base_url")).rstrip("/")
        api_key = _safe_str(cfg.get("api_key"))
        if not (model and base_url and api_key):
            raise SdkError("尚未配置可用的模型（含视觉模型），请先在 N.E.K.O 的模型设置里配置。")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        limit = min(self.llm_timeout, 90.0)
        try:
            from utils.llm_client import create_chat_llm_async

            llm = create_chat_llm_async(
                model=model,
                base_url=base_url,
                api_key=api_key,
                max_completion_tokens=2048,
                timeout=limit,
            )
            result = await asyncio.wait_for(llm.ainvoke(messages), timeout=limit)
            text = getattr(result, "content", None) or _safe_str(result)
            if text.strip():
                return text.strip()
        except SdkError:
            raise
        except Exception as exc:
            self.logger.warning("[study_copilot] 视觉模型 client 调用失败，降级直连: %s", exc)
        return await asyncio.to_thread(self._llm_http_messages, base_url, api_key, model, messages, limit)

    @staticmethod
    def _image_to_data_url(item: dict[str, Any]) -> str:
        """把捕获到的图片部件转成 data URL（支持内存字节 / 本地路径 / http 地址）。"""
        import base64
        import urllib.request

        mime = _safe_str(item.get("mime")) or "image/png"
        raw = item.get("data")
        if isinstance(raw, (bytes, bytearray)):
            return f"data:{mime};base64,{base64.b64encode(bytes(raw)).decode('ascii')}"
        locator = _safe_str(item.get("path") or item.get("file") or item.get("url"))
        if not locator:
            raise SdkError("这张截图的图片来源认不出来，麻烦重发一次喵。")
        if locator.startswith("http://") or locator.startswith("https://"):
            with urllib.request.urlopen(locator, timeout=15) as resp:  # noqa: S310 - 宿主内网地址
                raw = resp.read()
        else:
            raw = Path(locator).read_bytes()
        kind = "image/png" if locator.lower().endswith(".png") else mime
        return f"data:{kind};base64,{base64.b64encode(raw).decode('ascii')}"

    async def _vision_diagnose(self, question: str, subject: str = "") -> str:
        """识图 → 转写 → 走常规诊断链路。"""
        transcript = await self._vision_transcribe(question)
        profile = self._effective_profile()
        header = f"已读取你发的截图并转写如下：\n{transcript[:1200]}"
        detail = await self._diagnose(transcript, subject)
        await asyncio.to_thread(
            self.store.add_session,
            "vision",
            f"识图诊断｜{_safe_str(profile.get('exam_type'))}｜题目字数 {len(transcript)}",
        )
        return f"{header}\n\n{'=' * 8} 诊断 {'=' * 8}\n{detail}"

    # ── 联网检索 ──────────────────────────────────────────────
    async def _search_resources(self, query: str, subject: str = "", point_id: str = "") -> str:
        if not self.network_enabled or not self.free_source_search or self.searcher is None:
            return ""
        items = await asyncio.to_thread(self.searcher.search, query, None, 4)
        if not items:
            return ""
        rows = [item.as_dict() for item in items]
        try:
            await asyncio.to_thread(self.store.save_resources, point_id, rows)
        except Exception:
            pass
        return format_resources(items, limit=6)

    async def _resolve_point(self, topic: str, subject: str = "") -> Any:
        """定位知识点：本地图谱优先，找不到就请大模型从描述里推断一个名字。"""
        profile = self._effective_profile()
        exam_type = profile["exam_type"]
        if not subject:
            subject = self._subjects_list(profile)[0] if self._subjects_list(profile) else ""
        point = find_point(topic, subject, exam_type)
        if point:
            return point
        candidates = match_points(topic, subject, exam_type, limit=3)
        if candidates:
            return candidates[0]
        # 让大模型把用户的说法翻译成图谱里的知识点名
        known = "、".join(item.name for item in points_for(subject, exam_type)[:40])
        system = (
            "你是考点索引助手。下面给出了该科目已知的考点名单，"
            "请从用户描述里挑出最接近的一个考点名称；如果都不合适，就返回最接近的通用章节名。\n"
            "只返回一个 JSON：{\"name\":\"考点名\"}"
        )
        user = f"用户描述：{topic}\n已知考点：{known}"
        try:
            payload = await self._llm_json(system, user, timeout=min(30.0, self.llm_timeout))
            name = _safe_str((payload or {}).get("name"))
            if name:
                found = find_point(name, subject, exam_type)
                if found:
                    return found
                hits = match_points(name, subject, exam_type, limit=1)
                if hits:
                    return hits[0]
        except Exception as exc:
            self.logger.warning("[study_copilot] 知识点推断失败: %s", exc)
        return None

    # ── 面板 ──────────────────────────────────────────────────
    def _panel_html(self) -> str:
        path = self.static_dir / "index.html"
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return "<html><body><p>面板页面缺失喵。</p></body></html>"

    def _start_panel(self) -> None:
        endpoints = {
            ("GET", "/api/status"): self._api_status,
            ("POST", "/api/profile"): self._api_profile,
            ("POST", "/api/plan"): self._api_plan,
            ("POST", "/api/quiz"): self._api_quiz,
            ("POST", "/api/teach"): self._api_teach,
            ("POST", "/api/diagnose"): self._api_diagnose,
            ("POST", "/api/vision"): self._api_vision,
            ("POST", "/api/forecast"): self._api_forecast,
            ("POST", "/api/comfort"): self._api_comfort,
            ("POST", "/api/search"): self._api_search,
            ("POST", "/api/platforms"): self._api_platforms,
            ("POST", "/api/connect"): self._api_connect,
            ("POST", "/api/disconnect"): self._api_disconnect,
            ("POST", "/api/sync"): self._api_sync,
        }
        port = find_open_port(self.panel_port)
        server = PanelServer(port, self._panel_html, endpoints)
        if server.start():
            self._panel_server = server
            self.logger.info("[study_copilot] 面板已启动: http://127.0.0.1:{}", port)
            try:
                self.register_static_ui("static")
            except Exception as exc:
                self.logger.warning("[study_copilot] static UI 注册失败: %s", exc)
        else:
            self.logger.warning("[study_copilot] 面板启动失败（端口占用）")

    def _api_status(self, _body: dict) -> dict:
        profile = self._effective_profile()
        stored = self.store.get_profile()
        model = self._model_config()
        platforms = self.connectors.available() if self.connectors else []
        return {
            "ok": True,
            "port": self._panel_server.port if self._panel_server else self.panel_port,
            "profile": profile,
            "stored": bool(stored),
            "exam_types": list_exam_types(),
            "sources": list_sources(),
            "platforms": platforms,
            "overview": self.store.overview(),
            "latest_plan": self.store.latest_plan(),
            "latest_diagnosis": self.store.latest_diagnosis(),
            "network": {
                "enabled": self.network_enabled,
                "free_source_search": self.free_source_search,
                "platform_sync": self.platform_sync_enabled,
                "robots": self.crawl_respect_robots,
                "interval_ms": self.crawl_interval_ms,
            },
            "model": {
                "model": _safe_str(model.get("model")),
                "base_url": _safe_str(model.get("base_url")),
                "configured": bool(model.get("model") and model.get("base_url") and model.get("api_key")),
            },
            "psychology": {"enabled": self.psychology_enabled, "level": self.psychology_level},
        }

    def _api_profile(self, body: dict) -> dict:
        payload = {key: value for key, value in (body or {}).items() if value not in (None, "")}
        if payload.get("exam_type"):
            payload["exam_type"] = resolve_exam_type(_safe_str(payload.get("exam_type")))
        saved = self.store.save_profile(payload)
        return {"ok": True, "profile": self._effective_profile(), "saved": saved}

    def _run_async(self, factory, timeout: float = 120.0) -> dict:
        """面板线程里安全地跑一段协程，统一成 dict 返回给前端。

        注意：这里**不能**再用 ``if not bridge.ready(): return "尚未就绪"`` 提前短路。
        宿主可能把 startup 跑在临时循环里，那种情况下 "没就绪" 是常态而不是错误，
        直接短路会让面板永远不可用；现在由 :class:`AsyncBridge` 自己兜底
        （宿主循环死了就用插件私有循环），真出不来才报错，并把诊断信息带出来。
        """
        try:
            result = self._bridge.call(factory, timeout=timeout)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "bridge": self._bridge.describe()}
        if isinstance(result, dict):
            return result
        return {"ok": True, "text": result}

    def _api_plan(self, body: dict) -> dict:
        horizon = _safe_str((body or {}).get("horizon"), "long")
        return self._run_async(lambda: self._plan(horizon))

    def _api_quiz(self, body: dict) -> dict:
        payload = body or {}
        topic = _safe_str(payload.get("topic"))
        subject = _safe_str(payload.get("subject"))
        stage = _safe_str(payload.get("stage"), "auto")
        count = _safe_int(payload.get("count"), self.quiz_count)
        return self._run_async(lambda: self._quiz(topic, subject, stage, count))

    def _api_teach(self, body: dict) -> dict:
        payload = body or {}
        topic = _safe_str(payload.get("topic"))
        subject = _safe_str(payload.get("subject"))
        stage = _safe_str(payload.get("stage"), "auto")
        return self._run_async(lambda: self._teach(topic, subject, stage))

    def _api_diagnose(self, body: dict) -> dict:
        payload = body or {}
        text = _safe_str(payload.get("text"))
        subject = _safe_str(payload.get("subject"))
        return self._run_async(lambda: self._diagnose(text, subject))

    def _api_vision(self, body: dict) -> dict:
        """面板上传截图（base64）→ 识图诊断；也支持用聊天里最近捕获的图。"""
        payload = body or {}
        subject = _safe_str(payload.get("subject"))
        question = _safe_str(payload.get("question"))
        image_b64 = _safe_str(payload.get("image_base64")).strip()
        if image_b64:
            import base64
            import binascii

            header, _, b64_data = image_b64.partition(",")
            mime = "image/png"
            if header.startswith("data:"):
                mime = header[5:].split(";", 1)[0] or mime
                b64_data = b64_data or header
            try:
                raw = base64.b64decode(b64_data, validate=False)
            except (binascii.Error, ValueError):
                return {"ok": False, "error": "图片数据不是合法的 base64，请重新选择文件。"}
            if len(raw) > 12 * 1024 * 1024:
                return {"ok": False, "error": "图片超过 12MB，先压一压再发喵。"}
            self.last_capture = {
                "text": _safe_str(self.last_capture.get("text")),
                "images": [{"data": raw, "mime": mime}],
                "ts": time.time(),
            }
        return self._run_async(lambda: self._vision_diagnose(question, subject))

    def _api_forecast(self, _body: dict) -> dict:
        return self._run_async(self._forecast)

    def _api_comfort(self, body: dict) -> dict:
        text = _safe_str((body or {}).get("text"))
        return self._run_async(lambda: self._comfort(text))

    def _api_search(self, body: dict) -> dict:
        payload = body or {}
        query = _safe_str(payload.get("query"))
        subject = _safe_str(payload.get("subject"))
        return self._run_async(lambda: self._search(query, subject))

    def _api_platforms(self, _body: dict) -> dict:
        if self.connectors is None:
            return {"ok": False, "error": "平台组件未初始化"}
        return {"ok": True, "platforms": self.connectors.available()}

    def _api_connect(self, body: dict) -> dict:
        if not self.platform_sync_enabled:
            return {"ok": False, "error": "平台同步已在配置里关闭"}
        if self.connectors is None:
            return {"ok": False, "error": "平台组件未初始化"}
        payload = body or {}
        return self.connectors.connect(
            _safe_str(payload.get("platform")),
            _safe_str(payload.get("username")),
            _safe_str(payload.get("password")),
            _safe_str(payload.get("captcha")),
            _safe_str(payload.get("base_url")),
            remember=_safe_bool(payload.get("remember"), True),
        )

    def _api_disconnect(self, body: dict) -> dict:
        if self.connectors is None:
            return {"ok": False, "error": "平台组件未初始化"}
        return self.connectors.disconnect(_safe_str((body or {}).get("platform")))

    def _api_sync(self, body: dict) -> dict:
        if self.connectors is None:
            return {"ok": False, "error": "平台组件未初始化"}
        payload = body or {}
        platform = _safe_str(payload.get("platform"))
        base_url = _safe_str(payload.get("base_url"))
        return self._run_async(lambda: self._sync_platform(platform, base_url), timeout=150.0)

    # ── 内部业务 ──────────────────────────────────────────────
    async def _plan(self, horizon: str = "long") -> str:
        await self._ensure_ready()
        profile = self._effective_profile()
        mastery = await asyncio.to_thread(self.store.mastery_map)
        plan = build_plan(profile, mastery, horizon=horizon or "long")
        await asyncio.to_thread(
            self.store.save_plan,
            f"{get_profile(profile['exam_type']).name} · 剩余 {plan.days_left} 天",
            "",
            plan.exam_date,
            plan.as_dict(),
        )
        return format_plan(plan)

    async def _diagnose(self, text: str, subject: str = "") -> str:
        await self._ensure_ready()
        profile = self._effective_profile()
        result = await asyncio.to_thread(diagnose, self.store, profile, text, subject, profile["exam_type"])
        base = format_diagnosis(result)
        if not text:
            await asyncio.to_thread(self.store.save_diagnosis, result.summary, result.as_dict())
            return base
        system = (
            "你是资深备考诊断师。结合学生给的材料、所在地区与学校情况，做一份务实的诊断：\n"
            "1. 这份材料暴露了哪些具体问题（按知识点归类，不要泛泛而谈）；\n"
            "2. 其中哪些是『会但做错』（步骤/习惯问题），哪些是『真的不会』（知识漏洞）；\n"
            "3. 按提分性价比排出接下来该补的 3 个知识点，并说明为什么是它们；\n"
            "4. 给出针对这个地区/这份卷子的具体提醒。\n"
            "不要吹捧，也不要打击，说实话。"
        )
        user = (
            f"考试：{get_profile(profile['exam_type']).name}｜地区：{profile.get('region') or '未填写'}"
            f"｜学校：{profile.get('school') or '未填写'}｜年级：{profile.get('grade') or '未填写'}\n"
            f"本地扫描结果：\n{base}\n\n"
            f"学生提供的材料（可能是题目、试卷、成绩或一段自述）：\n{text[:4000]}"
        )
        try:
            detail = await self._llm_chat(system, user)
        except SdkError:
            detail = "（模型不可用，以上为本地扫描结果）"
        payload = result.as_dict()
        payload["detail"] = detail
        await asyncio.to_thread(self.store.save_diagnosis, result.summary, payload)
        return f"{base}\n\n{'=' * 8} 深度分析 {'=' * 8}\n{detail}"

    async def _forecast(self) -> str:
        await self._ensure_ready()
        profile = self._effective_profile()
        mastery = await asyncio.to_thread(self.store.mastery_map)
        days = self._days_left(profile)
        forecast = build_forecast(profile, mastery, days)
        return format_forecast(forecast)

    async def _teach(self, topic: str, subject: str = "", stage: str = "auto") -> str:
        await self._ensure_ready()
        if not topic:
            raise SdkError("想让我讲什么？给个知识点或者贴一段题目喵。")
        profile = self._effective_profile()
        point = await self._resolve_point(topic, subject)
        if point is None:
            raise SdkError(f"没能定位到知识点『{topic}』，换个更具体的说法试试喵。")
        mastery_map = await asyncio.to_thread(self.store.mastery_map)
        level = float(mastery_map.get(point.id, 0.5))
        resolved_stage = decide_stage(level, stage or self.quiz_stage)
        resources = ""
        if self.network_enabled and self.free_source_search:
            resources = await self._search_resources(
                build_query(point.name, _safe_str(subject), profile["exam_type"]),
                point.subject,
                point.id,
            )
        context = TeachContext(
            point=point,
            stage=resolved_stage,
            mastery=level,
            style=self.teach_style,
            exam_name=get_profile(profile["exam_type"]).name,
            region=_safe_str(profile.get("region")),
        )
        system, user = build_teach_prompt(context, resources)
        text = await self._llm_chat(system, user)
        await asyncio.to_thread(self.store.add_session, "teach", f"{point.name}｜{resolved_stage}")
        return f"{format_point(point)}\n\n{'=' * 8} 讲解 {'=' * 8}\n{text}"

    async def _quiz(self, topic: str, subject: str = "", stage: str = "auto", count: int = 0) -> str:
        await self._ensure_ready()
        if not topic:
            raise SdkError("要出哪个知识点的题？说个名字喵。")
        profile = self._effective_profile()
        point = await self._resolve_point(topic, subject)
        if point is None:
            raise SdkError(f"没能定位到知识点『{topic}』，换个更具体的说法试试喵。")
        mastery_map = await asyncio.to_thread(self.store.mastery_map)
        level = float(mastery_map.get(point.id, 0.5))
        resolved_stage = decide_stage(level, stage or self.quiz_stage)
        ok, warning = validate_question_plan(point, resolved_stage)
        if not ok:
            raise SdkError(warning)
        total = count or self.quiz_count
        resources = ""
        if self.network_enabled and self.free_source_search:
            resources = await self._search_resources(
                build_query(point.name, point.subject, profile["exam_type"]), point.subject, point.id
            )
        system, user = build_quiz_prompt(
            point,
            resolved_stage,
            total,
            get_profile(profile["exam_type"]).name,
            _safe_str(profile.get("region")),
            self.quiz_with_traps,
            resources,
        )
        payload = await self._llm_json(system, user)
        questions = _extract_questions(payload)
        if not questions:
            raise SdkError("模型这次没吐出合法的题目，再试一次喵。")
        await asyncio.to_thread(self.store.add_session, "quiz", f"{point.name}｜{resolved_stage}｜{len(questions)} 题")
        head = quiz_intro(point, resolved_stage, len(questions))
        if warning:
            head += f"\n{warning}"
        return f"{head}\n{format_questions(questions)}"

    async def _grade(self, topic: str, question: str, answer: str) -> str:
        await self._ensure_ready()
        if not (question and answer):
            raise SdkError("把题目和你的答案都发给我喵。")
        profile = self._effective_profile()
        # 没给知识点名时，用题干去反查，别直接抛错
        point = await self._resolve_point(topic or question[:120], "")
        if point is None:
            raise SdkError(f"没能定位到知识点『{topic or '（未指定）'}』，告诉我是哪一章的喵。")
        system, user = build_grade_prompt(point, question, answer, get_profile(profile["exam_type"]).name)
        payload = await self._llm_json(system, user)
        data = payload if isinstance(payload, dict) else {}
        correct = _safe_bool(data.get("correct"), False)
        level = await asyncio.to_thread(self.store.update_mastery, point.id, point.subject, correct)
        advice = _safe_str(data.get("advice"))
        reason = _safe_str(data.get("reason"))
        lines = [
            f"判定：{'对了' if correct else '错了'}｜{point.name} 掌握度更新为 {level:.0%}",
        ]
        if reason:
            lines.append(f"原因：{reason}")
        if advice:
            lines.append(f"建议：{advice}")
        return "\n".join(lines)

    async def _search(self, query: str, subject: str = "") -> str:
        await self._ensure_ready()
        if not query:
            raise SdkError("想找什么资料？给个关键词喵。")
        if not (self.network_enabled and self.free_source_search):
            raise SdkError("免费资源检索当前是关闭的，可以在面板或配置里打开。")
        profile = self._effective_profile()
        full_query = build_query(query, subject, profile["exam_type"])
        text = await self._search_resources(full_query, subject)
        if not text:
            return "这次没检索到公开资源喵，换个关键词，或者检查一下网络。"
        return f"检索词：{full_query}\n\n{text}"

    async def _comfort(self, text: str = "") -> str:
        await self._ensure_ready()
        if not self.psychology_enabled:
            raise SdkError("心理疏导当前是关闭的。")
        profile = self._effective_profile()
        mastery = await asyncio.to_thread(self.store.mastery_map)
        average = sum(mastery.values()) / len(mastery) if mastery else 0.5
        result = counsel(
            profile["exam_type"],
            self._days_left(profile),
            average,
            mood="",
            text=text,
            level=self.psychology_level,
        )
        if result.risk:
            await asyncio.to_thread(self.store.add_session, "comfort-risk", "检测到风险信号")
            return format_counsel(result)
        system, user = build_comfort_prompt(result, self.catgirl_name, self.psychology_level)
        try:
            words = await self._llm_chat(system, user, timeout=min(45.0, self.llm_timeout))
        except SdkError:
            words = format_counsel(result)
        await asyncio.to_thread(self.store.add_session, "comfort", result.mood_label)
        return words

    async def _sync_platform(self, platform_id: str, base_url: str = "") -> dict:
        await self._ensure_ready()
        if not self.platform_sync_enabled:
            return {"ok": False, "error": "平台同步已在配置里关闭"}
        if self.connectors is None:
            return {"ok": False, "error": "平台组件未初始化"}
        if not platform_id:
            return {"ok": False, "error": "请指定要同步的平台"}
        raw = await asyncio.to_thread(self.connectors.fetch_raw, platform_id, base_url)
        if not raw.get("ok"):
            return raw
        pages = [page for page in raw.get("pages", []) if page.get("ok")]
        if not pages:
            return {"ok": False, "error": "登录后没有读到任何页面，可能是登录态过期或页面结构已变化"}
        joined = "\n\n".join(f"[{page['url']}]\n{page.get('text', '')[:2500]}" for page in pages[:3])
        system = (
            "你把学习平台的页面文本整理成结构化数据。只整理页面里真实出现的内容，不要编造。\n"
            "返回 JSON：{\"courses\":[{\"name\":\"课程名\",\"progress\":\"进度描述或百分比\","
            "\"pending\":\"待办数量或描述\"}],\"summary\":\"一句话总结当前学习状态\","
            "\"suggestions\":[\"两条最该先处理的事\"]}"
        )
        try:
            payload = await self._llm_json(system, joined)
        except SdkError as exc:
            return {"ok": False, "error": str(exc), "pages": len(pages)}
        data = payload if isinstance(payload, dict) else {}
        await asyncio.to_thread(self.store.add_session, "sync", f"{platform_id}｜{_safe_str(data.get('summary'))}")
        return {"ok": True, "platform": platform_id, "data": data, "pages": len(pages)}

    # ── 生命周期 ──────────────────────────────────────────────
    @lifecycle(id="startup")
    async def on_startup(self) -> None:
        try:
            await self._startup_inner()
        except Exception as exc:  # 启动失败不要让整个插件进程被判死
            _dump_crash("startup")
            self.logger.warning("[study_copilot] 启动过程出错，已降级运行: %s", exc)

    async def _startup_inner(self) -> None:
        await self._load_config()
        self._loop = asyncio.get_running_loop()
        self._bridge.bind(self._loop)
        if self.network_enabled:
            self.crawler = Crawler(
                self.data_dir,
                interval_ms=self.crawl_interval_ms,
                respect_robots=self.crawl_respect_robots,
                timeout=self.crawl_timeout,
                max_pages=self.crawl_max_pages,
            )
            self.connectors = ConnectorManager(self.crawler, self.data_dir, self.admin_password)
            self.searcher = ResourceSearcher(self.crawler)
        self._start_panel()
        self.logger.info("[study_copilot] 事件循环桥：%s", self._bridge.describe())
        self.logger.info(
            "[study_copilot] 启动：考试=%s 地区=%s 科目=%s 目标=%s 每天=%d 分钟｜联网=%s",
            self.exam_type,
            self.region or "未填",
            self.subjects or "默认",
            self.target_score or "未设",
            self.daily_minutes,
            self.network_enabled,
        )

    @lifecycle(id="shutdown")
    async def on_shutdown(self) -> None:
        if self._panel_server:
            self._panel_server.stop()
        if self.crawler is not None:
            await asyncio.to_thread(self.crawler.save_cookies)
        self.logger.info("[study_copilot] 关闭")

    # ── 入口 ──────────────────────────────────────────────────
    @llm_tool(
        name="study_status",
        description="查看学习助手当前状态：考试档案、掌握度、平台连接、计划与诊断概览。",
        parameters={"type": "object", "properties": {}},
        timeout=30.0,
    )
    @plugin_entry(
        id="study_status",
        name="学习助手状态",
        description="返回档案、平台连接、资源检索与计划进度概览。",
        input_schema={"type": "object", "properties": {}},
    )
    async def status_entry(self, **_):
        await self._ensure_ready()
        profile = self._effective_profile()
        overview = await asyncio.to_thread(self.store.overview)
        region = profile.get("region") or "未填写"
        school = profile.get("school") or "未填写"
        grade = profile.get("grade") or "未填写"
        exam_date = profile.get("exam_date") or "未填写"
        lines = [
            f"考试：{get_profile(profile['exam_type']).name}",
            f"地区：{region}｜学校：{school}｜年级：{grade}",
            f"科目：{'、'.join(subject_label(item) for item in self._subjects_list(profile))}",
            f"目标分：{profile.get('target_score') or '未设定'}｜考试日期：{exam_date}｜还剩 {self._days_left(profile)} 天",
            f"每日可用：{profile.get('daily_minutes')} 分钟",
            f"已跟踪知识点：{overview['tracked_points']} 个｜平均掌握度：{overview['average_mastery']:.0%}",
            f"作答记录：{overview['attempts']['total']} 次，正确率 {overview['attempts']['rate']:.0%}",
            f"待复习：{overview['due_reviews']} 项｜已保存计划：{'有' if overview['plan'] else '无'}",
        ]
        if self.connectors is not None:
            connected = [row["id"] for row in self.connectors.available() if row.get("connected")]
            lines.append(f"已连接平台：{'、'.join(connected) if connected else '无'}")
        return Ok("\n".join(lines))

    @llm_tool(
        name="study_profile",
        description="查看或修改学习档案：目标考试、地区、学校、年级、科目、目标分、考试日期、每日学习分钟数。",
        parameters={
            "type": "object",
            "properties": {
                "exam_type": {
                    "type": "string",
                    "description": "考试类型。可选：gaokao 高考 / zhongkao 中考 / cet4 四级 / cet6 六级 / "
                                   "kaoyan 考研 / final 校内通用 / university 大学期末(学分制,GPA) / "
                                   "cert_teacher 教资 / cert_soft 软考 / cert_cpa 注会 / cert_law 法考 / "
                                   "ielts 雅思 / toefl 托福 / ncse 计算机等级考试。也可直接写中文，"
                                   "如『大学期末』『绩点』『保研』『教资』『软考』『注会』『法考』『雅思』",
                },
                "credits": {
                    "type": "string",
                    "description": "大学学分制专用：科目与学分的对应关系，形如 'math_adv:5,english:3,politics:3'。"
                                   "填了它，时间分配与绩点预估才会按学分加权。",
                },
                "region": {"type": "string", "description": "省份或直辖市，例如 广东"},
                "school": {"type": "string", "description": "学校名称"},
                "grade": {"type": "string", "description": "年级，例如 高三"},
                "subjects": {"type": "string", "description": "科目，英文 key 逗号分隔，如 chinese,math,english"},
                "target_score": {"type": "number", "description": "目标总分"},
                "exam_date": {"type": "string", "description": "考试日期 YYYY-MM-DD"},
                "daily_minutes": {"type": "integer", "description": "每天可用于该考试的有效分钟数"},
            },
        },
        timeout=30.0,
    )
    @plugin_entry(
        id="study_profile",
        name="学习档案",
        description="查看或修改考试目标、地区、科目、目标分与可用时间。",
        input_schema={"type": "object", "properties": {}},
    )
    async def profile_entry(
        self,
        exam_type: str = "",
        region: str = "",
        school: str = "",
        grade: str = "",
        subjects: str = "",
        target_score: float = 0,
        exam_date: str = "",
        daily_minutes: int = 0,
        credits: str = "",
        **_,
    ):
        await self._ensure_ready()
        payload = {
            key: value
            for key, value in {
                "exam_type": resolve_exam_type(exam_type) if exam_type else "",
                "region": region,
                "school": school,
                "grade": grade,
                "subjects": subjects,
                "target_score": target_score,
                "exam_date": exam_date,
                "daily_minutes": daily_minutes,
                "credits": credits,
            }.items()
            if value not in ("", 0, None)
        }
        if payload:
            await asyncio.to_thread(self.store.save_profile, payload)
        profile = self._effective_profile()
        overview = format_profile(get_profile(profile["exam_type"]), self._subjects_list(profile))
        return Ok(
            "当前档案：\n"
            f"考试：{get_profile(profile['exam_type']).name}\n"
            f"地区：{profile.get('region') or '未填写'}｜学校：{profile.get('school') or '未填写'}"
            f"｜年级：{profile.get('grade') or '未填写'}\n"
            f"科目：{'、'.join(subject_label(item) for item in self._subjects_list(profile))}\n"
            f"目标分：{profile.get('target_score') or '未设定'}｜考试日期：{profile.get('exam_date') or '未填写'}"
            f"｜还剩 {self._days_left(profile)} 天\n"
            f"每日可用：{profile.get('daily_minutes')} 分钟\n"
            f"学分：{profile.get('credits') or '未填写（大学考试建议填，例：math_adv:5,english:3）'}\n\n"
            f"卷子结构：\n{overview}"
        )

    @llm_tool(
        name="study_plan",
        description="生成学习计划。用户说『帮我规划一下』『怎么安排接下来的复习』『这周学什么』时调用。",
        parameters={
            "type": "object",
            "properties": {
                "horizon": {"type": "string", "description": "long 长期（到考试）或 short 短期（本周）"},
            },
        },
        timeout=60.0,
    )
    @plugin_entry(
        id="study_plan",
        name="生成学习计划",
        description="按目标考试、目标分、剩余天数和每天可用时间生成长期与短期学习计划。",
        input_schema={"type": "object", "properties": {"horizon": {"type": "string"}}},
    )
    async def plan_entry(self, horizon: str = "long", **_):
        try:
            return Ok(await self._plan(horizon))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_diagnose",
        description=(
            "诊断学习情况。用户提供文字版题目、试卷、成绩单，或问『我哪里薄弱』"
            "『这套卷子暴露了什么问题』时调用。注意：如果用户是发截图而不是打字，"
            "请改用 study_vision 识图诊断。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "题目、试卷、成绩或一段自述"},
                "subject": {"type": "string", "description": "科目 key，如 math；可留空自动判断"},
            },
        },
        timeout=90.0,
    )
    @plugin_entry(
        id="study_diagnose",
        name="学习诊断",
        description="根据题目、试卷、地区与学校推断薄弱知识点，输出诊断报告。",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    async def diagnose_entry(self, text: str = "", subject: str = "", **_):
        try:
            return Ok(await self._diagnose(text, subject))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_vision",
        description=(
            "识图学习辅助：用户在聊天里发过题目/试卷/作业/成绩单的截图，并让你讲题、"
            "批改、诊断薄弱点或估分时调用。会读取用户最近发的截图，用视觉模型转写成文字，"
            "再走常规诊断链路。用户没发过截图时不要调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "用户想对这张图问什么，例如『第 3 题怎么做』『这套卷子我哪里弱』"},
                "subject": {"type": "string", "description": "科目 key，如 math；可留空自动判断"},
            },
        },
        timeout=120.0,
    )
    @plugin_entry(
        id="study_vision",
        name="识图诊断",
        description="读取用户最近发的题目/试卷截图，转写后做薄弱点诊断与讲解。",
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string"}, "subject": {"type": "string"}},
        },
    )
    async def vision_entry(self, question: str = "", subject: str = "", **_):
        await self._ensure_ready()
        if not self.vision_enabled:
            raise SdkError("识图功能当前是关闭的，可以在配置里把 vision_enabled 打开。")
        try:
            return Ok(await self._vision_diagnose(question, subject))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_forecast",
        description="预估分数。用户问『我能考多少分』『能不能过』『大概什么水平』时调用，给出保守/最可能/理想三档。",
        parameters={"type": "object", "properties": {}},
        timeout=60.0,
    )
    @plugin_entry(
        id="study_forecast",
        name="分数预估",
        description="给出保守、最可能与理想三档分数预期及依据。",
        input_schema={"type": "object", "properties": {}},
    )
    async def forecast_entry(self, **_):
        try:
            return Ok(await self._forecast())
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_teach",
        description="讲解知识点并配套例题。用户说『讲一下 XX』『这个我不懂』时调用。",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "知识点名称，例如 导数及其应用"},
                "subject": {"type": "string", "description": "科目 key，如 math；可留空"},
                "stage": {"type": "string", "description": "basic/medium/hard/auto"},
            },
        },
        timeout=90.0,
    )
    @plugin_entry(
        id="study_teach",
        name="知识点讲解",
        description="讲解知识点并配套分阶段的例题，必要时先补前置知识。",
        input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
    )
    async def teach_entry(self, topic: str = "", subject: str = "", stage: str = "auto", **_):
        try:
            return Ok(await self._teach(topic, subject, stage))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_quiz",
        description="按知识点命题规律出题。用户说『出几道题』『考考我』『给我练练』时调用。",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "知识点名称"},
                "subject": {"type": "string", "description": "科目 key，可留空"},
                "stage": {"type": "string", "description": "basic/medium/hard/auto"},
                "count": {"type": "integer", "description": "题量，默认 3"},
            },
        },
        timeout=90.0,
    )
    @plugin_entry(
        id="study_quiz",
        name="出题测验",
        description="按知识点命题规律出基础、中等易错或综合创新题。",
        input_schema={"type": "object", "properties": {"topic": {"type": "string"}}},
    )
    async def quiz_entry(self, topic: str = "", subject: str = "", stage: str = "auto", count: int = 0, **_):
        try:
            return Ok(await self._quiz(topic, subject, stage, count))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_grade",
        description="批改学生的作答并更新掌握度。学生给出答案后调用。",
        parameters={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "知识点名称"},
                "question": {"type": "string", "description": "题目原文"},
                "answer": {"type": "string", "description": "学生的答案"},
            },
        },
        timeout=90.0,
    )
    @plugin_entry(
        id="study_grade",
        name="批改作答",
        description="批改一道题的作答，按真实给分标准判定并更新掌握度。",
        input_schema={"type": "object", "properties": {"answer": {"type": "string"}}},
    )
    async def grade_entry(self, topic: str = "", question: str = "", answer: str = "", **_):
        try:
            return Ok(await self._grade(topic, question, answer))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_search",
        description="检索免费公开学习资源（国家智慧教育平台、中国大学MOOC、学堂在线、B站学习区等）。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词，例如 导数 单调性"},
                "subject": {"type": "string", "description": "科目 key，可留空"},
            },
        },
        timeout=90.0,
    )
    @plugin_entry(
        id="study_search",
        name="检索学习资源",
        description="在免费公开课程与题库资源中检索匹配当前知识点的资料。",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
    )
    async def search_entry(self, query: str = "", subject: str = "", **_):
        try:
            return Ok(await self._search(query, subject))
        except SdkError as exc:
            return Err(str(exc))

    @llm_tool(
        name="study_platform_sync",
        description="同步本人学习平台的课程、进度与作业（只读）。用户说『同步一下学习通』『看看我还有多少课没看』时调用。",
        parameters={
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "平台 id：chaoxing/zhihuishu/icourse163/xuetangx/jwc/neea"},
            },
        },
        timeout=120.0,
    )
    @plugin_entry(
        id="study_platform_sync",
        name="同步学习平台",
        description="同步本人学习平台账号的课程、进度与作业数据（只读）。",
        input_schema={"type": "object", "properties": {"platform": {"type": "string"}}},
    )
    async def sync_entry(self, platform: str = "", **_):
        result = await self._sync_platform(platform)
        if not result.get("ok"):
            return Err(_safe_str(result.get("error"), "同步失败"))
        data = result.get("data") or {}
        courses = data.get("courses") or []
        lines = [f"已同步 {result.get('platform')}（读取了 {result.get('pages')} 个页面）"]
        if _safe_str(data.get("summary")):
            lines.append(_safe_str(data.get("summary")))
        for course in courses[:10]:
            lines.append(
                f"  · {_safe_str(course.get('name'), '未命名课程')}：进度 {_safe_str(course.get('progress'), '未知')}"
                f"｜待办 {_safe_str(course.get('pending'), '无')}"
            )
        for suggestion in (data.get("suggestions") or [])[:3]:
            lines.append(f"  → {suggestion}")
        return Ok("\n".join(lines))

    @llm_tool(
        name="study_comfort",
        description="心理疏导与学习心理暗示。用户表达焦虑、疲惫、迷茫、拖延、考前恐慌，或考砸了的时候调用。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户当前的情绪表达，原样传入"},
            },
        },
        timeout=60.0,
    )
    @plugin_entry(
        id="study_comfort",
        name="心理疏导",
        description="按考试类型、临考阶段与情绪状态做个性化疏导和心理暗示。",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )
    async def comfort_entry(self, text: str = "", **_):
        try:
            return Ok(await self._comfort(text))
        except SdkError as exc:
            return Err(str(exc))

    @plugin_entry(
        id="study_review_queue",
        name="到期复习队列",
        description="列出按艾宾浩斯间隔到期的知识点，供今日复习使用。",
        input_schema={"type": "object", "properties": {}},
    )
    async def review_entry(self, **_):
        await self._ensure_ready()
        rows = await asyncio.to_thread(self.store.due_reviews, 10)
        if not rows:
            return Ok("今天没有到期的复习项喵，可以推进新内容。")
        lines = ["今天该复习这些："]
        for row in rows:
            point_id = _safe_str(row.get("point_id"))
            point = get_point(point_id)
            name = point.name if point else point_id
            tail = f"（{subject_label(point.subject)}）" if point else ""
            lines.append(f"  · {name}{tail}：当前掌握度 {float(row.get('level') or 0):.0%}")
        return Ok("\n".join(lines))

    @plugin_entry(
        id="study_roi",
        name="提分性价比排行",
        description="按『还差多少 × 分值权重 × 考频』排出最值得先补的知识点。",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
    )
    async def roi_entry(self, limit: int = 8, **_):
        await self._ensure_ready()
        profile = self._effective_profile()
        mastery = await asyncio.to_thread(self.store.mastery_map)
        rows = roi_ranking(profile["exam_type"], self._subjects_list(profile), mastery, limit or 8)
        if not rows:
            return Ok("还没有可排序的知识点喵。")
        lines = ["按提分性价比，先补这些："]
        for index, row in enumerate(rows, start=1):
            point = row["point"]
            lines.append(
                f"{index}. {point['name']}（{subject_label(point['subject'])}）性价比 {row['roi']}\n"
                f"   {row['reason']}\n"
                f"   只会考：{'、'.join(point['forms_label'])}"
            )
        return Ok("\n".join(lines))

    @plugin_entry(
        id="study_open_panel",
        name="打开学习面板",
        description="返回学习辅助面板的访问地址，用于设置档案、连接平台与检索资源。",
        input_schema={"type": "object", "properties": {}},
    )
    async def panel_entry(self, **_):
        port = self._panel_server.port if self._panel_server else self.panel_port
        return Ok(f"学习面板地址：http://127.0.0.1:{port} （本机可访问）")


__all__ = ["StudyCopilotPlugin"]
