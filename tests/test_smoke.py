"""学习辅助猫娘冒烟测试：结构契约 + 核心逻辑（不依赖 N.E.K.O SDK）"""

import importlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    """用虚拟包挂载子模块，绕开依赖宿主 SDK 的 __init__.py。"""
    pkg = sys.modules.get("neko_study_copilot")
    if pkg is None or not getattr(pkg, "__path__", None):
        pkg = types.ModuleType("neko_study_copilot")
        pkg.__path__ = [str(ROOT)]
        sys.modules["neko_study_copilot"] = pkg
    return importlib.import_module(f"neko_study_copilot.{name}")


class TestPluginStructure:
    """官方 check 要求入库的文件，一个都不能少。"""

    def test_plugin_toml_exists(self):
        assert (ROOT / "plugin.toml").is_file()

    def test_entry_declared(self):
        text = (ROOT / "plugin.toml").read_text(encoding="utf-8")
        assert 'id = "neko_study_copilot"' in text
        assert 'entry = "plugin.plugins.neko_study_copilot:StudyCopilotPlugin"' in text

    def test_required_repo_files(self):
        for relative in (
            "tests/test_smoke.py",
            ".vscode/settings.json",
            ".vscode/tasks.json",
            ".github/workflows/verify.yml",
            ".github/workflows/release.yml",
            "config.example.toml",
            "profiles/default.toml",
            "ruff.toml",
            "pyproject.toml",
            "static/index.html",
            "README.md",
        ):
            assert (ROOT / relative).is_file(), f"缺少 {relative}"

    def test_i18n_has_all_locales(self):
        locales = ("zh-CN", "zh-TW", "en", "ja", "ko", "ru", "es", "pt")
        base = None
        for locale in locales:
            path = ROOT / "i18n" / f"{locale}.json"
            assert path.is_file(), f"缺少 i18n/{locale}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data.get("plugin.name"), f"{locale} 缺少 plugin.name"
            if base is None:
                base = set(data)
            else:
                assert set(data) == base, f"{locale} 的 key 与其他语言不一致"

    def test_gitignore_does_not_block_required_dirs(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for blocked in ("tests/", ".vscode/", "i18n", "static", ".github"):
            assert blocked not in text, f".gitignore 不应排除 {blocked}"

    def test_credentials_never_committed(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "data/" in text, "data/ 必须被忽略（含账号凭据与学习数据）"
        assert "config/" in text, "config/ 必须被忽略"


class TestSyllabus:
    def test_points_have_forms(self):
        syllabus = load("_syllabus")
        assert len(syllabus.POINTS) > 80
        for point in syllabus.POINTS.values():
            assert point.forms, f"{point.name} 缺少题型声明"

    def test_form_constraint_rejects_impossible_question(self):
        profiles = load("_profiles")
        syllabus = load("_syllabus")
        recite = syllabus.find_point("名篇名句默写", "chinese", "gaokao")
        ok, message = syllabus.validate_form(recite, profiles.FORM_SOLVE)
        assert ok is False
        assert "填空题" in message

    def test_big_points_are_marked(self):
        syllabus = load("_syllabus")
        big = syllabus.big_points("math", "gaokao")
        names = {point.name for point in big}
        assert "导数及其应用" in names
        assert "解析几何（直线、圆、圆锥曲线）" in names


class TestPlanner:
    def test_allocation_follows_score_not_weight(self):
        planner = load("_planner")
        allocation = planner.allocate_minutes(["math", "physics"], "gaokao", {}, 180)
        assert allocation["math"] > allocation["physics"]

    def test_build_plan_has_phases(self):
        planner = load("_planner")
        plan = planner.build_plan(
            {"exam_type": "gaokao", "subjects": "math,physics", "daily_minutes": 180}, {}
        )
        assert plan.days_left > 0
        assert len(plan.phases) >= 2
        assert "阶段安排" in planner.format_plan(plan)

    def test_weekly_rotates_subjects(self):
        planner = load("_planner")
        plan = planner.build_plan(
            {"exam_type": "gaokao", "subjects": "chinese,math,english", "daily_minutes": 200}, {}
        )
        mains = {week["main_subject"] for week in plan.weekly[:3]}
        assert len(mains) > 1


class TestForecast:
    def test_three_tiers_are_ordered(self):
        diagnose = load("_diagnose")
        forecast = diagnose.build_forecast(
            {"exam_type": "gaokao", "subjects": "math,english", "daily_minutes": 180},
            {"math.高中.数列": 0.4},
            100,
        )
        assert forecast.conservative <= forecast.likely <= forecast.ideal
        assert forecast.ideal <= forecast.total_score

    def test_partial_subjects_scale_pass_line(self):
        diagnose = load("_diagnose")
        forecast = diagnose.build_forecast(
            {"exam_type": "gaokao", "subjects": "chinese,math", "daily_minutes": 180}, {}, 100
        )
        assert abs(forecast.total_score - 300) < 1
        assert forecast.pass_line is None or forecast.pass_line < 430

    def test_cet_pass_line(self):
        diagnose = load("_diagnose")
        forecast = diagnose.build_forecast(
            {"exam_type": "cet4", "subjects": "english", "daily_minutes": 60}, {}, 60
        )
        assert forecast.pass_line == 425


class TestUniversityAndCerts:
    """大学生（学分制 / 绩点）与证书类考试。"""

    def test_exam_alias_resolution(self):
        profiles = load("_profiles")
        assert profiles.resolve_exam_type("大学期末") == "university"
        assert profiles.resolve_exam_type("教资") == "cert_teacher"
        assert profiles.resolve_exam_type("CPA") == "cert_cpa"
        assert profiles.resolve_exam_type("法考") == "cert_law"
        assert profiles.resolve_exam_type("雅思") == "ielts"

    def test_gpa_conversion(self):
        profiles = load("_profiles")
        assert profiles.gpa_from_score(92, 4.0) == 4.0
        assert profiles.gpa_from_score(59, 4.0) == 0.0
        gpa, credits = profiles.weighted_gpa(
            [{"score": 90, "credit": 5}, {"score": 60, "credit": 1}], 4.0
        )
        assert credits == 6.0
        assert abs(gpa - 3.5) < 1e-6

    def test_credit_weighted_allocation(self):
        planner = load("_planner")
        allocation = planner.allocate_minutes(
            ["math_adv", "english"], "university",
            {"math_adv.大学.常微分方程": 0.2, "english.大学.仔细阅读": 0.9},
            180, {"math_adv": 5.0, "english": 1.0},
        )
        assert allocation["math_adv"] > allocation["english"]

    def test_university_forecast_has_gpa_and_risk(self):
        diagnose = load("_diagnose")
        forecast = diagnose.build_forecast(
            {"exam_type": "university", "subjects": "math_adv,english,politics",
             "credits": "math_adv:5,english:3,politics:3", "daily_minutes": 180},
            {"math_adv.大学.常微分方程": 0.1}, 30,
        )
        assert forecast.gpa is not None and 0 <= forecast.gpa <= 4.0
        assert forecast.pass_line == 180
        assert forecast.fail_risks and forecast.fail_risks[0]["subject"] == "math_adv"
        assert "学分加权绩点" in diagnose.format_forecast(forecast)

    def test_cert_forecast_uses_own_pass_line(self):
        diagnose = load("_diagnose")
        forecast = diagnose.build_forecast(
            {"exam_type": "cert_soft", "subjects": "morning,afternoon", "daily_minutes": 120}, {}, 60
        )
        assert forecast.pass_line == 90
        assert forecast.gpa is None

    def test_college_forms_registered(self):
        profiles = load("_profiles")
        syllabus = load("_syllabus")
        point = syllabus.find_point("中值定理与导数应用", "math_adv", "university")
        assert point is not None and profiles.FORM_PROOF in point.forms
        ok, _msg = syllabus.validate_form(point, profiles.FORM_CASE)
        assert ok is False

    def test_paper_subject_merges_to_syllabus(self):
        syllabus = load("_syllabus")
        assert syllabus.syllabus_key("quality") == "teacher"
        assert len(syllabus.points_for("quality", "cert_teacher")) >= 5


class TestStore:
    def test_mastery_updates(self, tmp_path):
        store_mod = load("_store")
        store = store_mod.StudyStore(tmp_path / "study.db")
        before = store.mastery_map().get("math.高中.数列", 0.5)
        up = store.update_mastery("math.高中.数列", "math", True)
        down = store.update_mastery("math.高中.数列", "math", False)
        assert up > before > 0
        assert down < up
        assert 0.0 <= down <= 1.0

    def test_profile_roundtrip(self, tmp_path):
        store_mod = load("_store")
        store = store_mod.StudyStore(tmp_path / "study.db")
        store.save_profile({"exam_type": "cet6", "region": "北京"})
        assert store.get_profile()["exam_type"] == "cet6"
        assert store.get_profile()["region"] == "北京"


class TestPsychology:
    def test_risk_detection_gives_hotlines(self):
        psych = load("_psych")
        result = psych.counsel("gaokao", 30, 0.5, text="整夜睡不着，觉得活不下去")
        assert result.risk is True
        assert "12355" in result.risk_text
        assert "400-161-9995" in result.risk_text

    def test_normal_mood_not_flagged(self):
        psych = load("_psych")
        result = psych.counsel("gaokao", 30, 0.5, text="今天数学没考好")
        assert result.risk is False

    def test_mood_detection(self):
        psych = load("_psych")
        assert psych.detect_mood("好焦虑") == "anxious"
        assert psych.detect_mood("学不动") == "tired"


class TestCrawler:
    def test_html_to_text_strips_scripts(self):
        crawl = load("_crawl")
        text = crawl.html_to_text(
            "<html><body><script>var a=1;</script><p>导数与极值</p></body></html>"
        )
        assert "导数与极值" in text
        assert "var a" not in text

    def test_json_extraction(self):
        crawl = load("_crawl")
        assert crawl.extract_json_object('x {"a": 1} y') == {"a": 1}
        assert crawl.extract_json_object("nothing") == {}

    def test_throttle_per_host(self):
        crawl = load("_crawl")
        throttle = crawl.Throttle(interval_ms=200)
        throttle.wait("a.com")
        throttle.wait("a.com")
        throttle.wait("b.com")  # 不同主机不应被上一次阻塞


class TestPlatforms:
    def test_builtin_platforms(self):
        platforms = load("_platforms")
        ids = {row["id"] for row in platforms.list_platforms()}
        assert {"chaoxing", "zhihuishu", "icourse163", "jwc"} <= ids

    def test_hidden_inputs_extracted(self):
        platforms = load("_platforms")
        hidden = platforms._extract_hidden_inputs(
            '<input type="hidden" name="lt" value="abc" />'
        )
        assert hidden["lt"] == "abc"

    def test_login_requires_credentials(self, tmp_path):
        crawl = load("_crawl")
        platforms = load("_platforms")
        crawler = crawl.Crawler(tmp_path, interval_ms=0, respect_robots=False, timeout=1.0)
        adapter = platforms.PlatformAdapter(platforms.PLATFORM_BY_ID["chaoxing"], crawler)
        assert adapter.login("", "")["status"] == "credential_missing"


class TestVision:
    """识图链路：聊天框发截图 → 捕获 → 转 data URL → 视觉模型转写。"""

    @staticmethod
    def _plugin_class():
        """加载真实的主模块（依赖 conftest 注入的 SDK stub）。"""
        import importlib.util

        cached = sys.modules.get("_nsc_main")
        if cached is not None:
            return cached.StudyCopilotPlugin
        spec = importlib.util.spec_from_file_location(
            "_nsc_main", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["_nsc_main"] = module
        spec.loader.exec_module(module)
        return module.StudyCopilotPlugin

    def _harness(self, plugin_cls):
        import logging

        class Harness(plugin_cls):  # 跳过构造函数，只测纯逻辑
            def __init__(self):
                self.last_capture = {"text": "", "images": [], "ts": 0.0}
                self.logger = logging.getLogger("neko_study_copilot_test")

        return Harness()

    def test_capture_text_and_image_parts(self):
        plugin_cls = self._plugin_class()
        obj = self._harness(plugin_cls)
        obj._capture_chat(
            {
                "parts": [
                    {"type": "text", "text": "这道题怎么做"},
                    {"type": "image", "data": b"\x89PNG", "mime": "image/png"},
                ]
            }
        )
        assert obj._capture_image_count() == 1
        assert "这道题怎么做" in obj.last_capture["text"]

    def test_capture_nested_payload(self):
        plugin_cls = self._plugin_class()
        obj = self._harness(plugin_cls)
        obj._capture_chat({"message": {"text": "帮我看看这张卷子"}})
        assert "帮我看看这张卷子" in obj.last_capture["text"]

    def test_capture_plain_text_fallback(self):
        plugin_cls = self._plugin_class()
        obj = self._harness(plugin_cls)
        obj._capture_chat("数学 92 分")
        assert "数学 92 分" in obj.last_capture["text"]

    def test_image_to_data_url(self):
        plugin_cls = self._plugin_class()
        url = plugin_cls._image_to_data_url({"data": b"\x89PNG", "mime": "image/jpeg"})
        assert url.startswith("data:image/jpeg;base64,")

    def test_vision_config_declared(self):
        for relative in ("plugin.toml", "config.example.toml", "profiles/default.toml"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            assert "vision_enabled" in text, f"{relative} 未声明 vision_enabled"

    def test_vision_entry_declared(self):
        text = (ROOT / "__init__.py").read_text(encoding="utf-8")
        assert 'id="study_vision"' in text


class TestFrozenHostCompat:
    """宿主是 PyInstaller 冻结进程，只打包它自己引用过的标准库。

    已实证：顶层 ``import urllib.robotparser`` 会让整个插件在 import 阶段死掉。
    所以任何"冷门"标准库都必须是 try/except 软导入 + 自带兜底。
    """

    RISKY = {"urllib.robotparser", "http.server", "gzip", "zlib", "html", "concurrent.futures"}

    def test_no_top_level_hard_import(self):
        import ast

        for path in sorted(ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:  # 只看模块顶层；try 体内的是软导入，合法
                module_names: list[str] = []
                if isinstance(node, ast.Import):
                    module_names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module_names = [node.module or ""]
                for name in module_names:
                    assert name not in self.RISKY, (
                        f"{path.name} 顶层硬导入 {name}；冻结宿主可能没打包它，"
                        "请改成 try/except 软导入并自带兜底"
                    )

    def test_import_survives_blocked_stdlib(self):
        """子进程里屏蔽掉 urllib.robotparser / http.server / html / gzip 后仍能 import。

        这就是线上故障的复现条件：宿主的冻结解释器没有 urllib.robotparser，
        顶层 import 它会让插件连模块体都执行不完，宿主日志里只剩
        PLUGIN_START_FAILED，连 traceback 都没有。
        """
        import subprocess
        import textwrap

        script = textwrap.dedent(
            f"""
            import builtins, importlib.util, sys, types

            BLOCKED = ("urllib.robotparser", "http.server", "html", "gzip")

            class Blocker:
                def find_module(self, fullname, path=None):
                    return self if self._hit(fullname) else None

                def find_spec(self, fullname, path=None, target=None):
                    if self._hit(fullname):
                        raise ModuleNotFoundError(f"No module named {{fullname!r}}")
                    return None

                @staticmethod
                def _hit(fullname):
                    return any(fullname == b or fullname.startswith(b + ".") for b in BLOCKED)

            sys.meta_path.insert(0, Blocker())
            # 先自证屏蔽器有效，否则这条测试就是假通过
            for probe in BLOCKED:
                try:
                    __import__(probe)
                except ModuleNotFoundError:
                    continue
                print("BLOCKER_NOT_WORKING:", probe)
                raise SystemExit(2)

            # 最小 SDK stub，模拟没有 N.E.K.O 的环境
            plugin_mod = types.ModuleType("plugin")
            sdk_mod = types.ModuleType("plugin.sdk")
            sdk_plugin = types.ModuleType("plugin.sdk.plugin")

            class NekoPluginBase:
                def __init__(self, ctx=None):
                    self.ctx = ctx

                def enable_file_logging(self, *a, **k):
                    import logging
                    return logging.getLogger("neko_study_copilot")

                def register_static_ui(self, *a, **k):
                    return True

                def data_path(self, *parts):
                    import tempfile, pathlib
                    return str(pathlib.Path(tempfile.gettempdir()).joinpath("nsc", *parts))

            def _deco(*args, **kwargs):
                if len(args) == 1 and not kwargs and callable(args[0]):
                    return args[0]
                def inner(fn):
                    return fn
                return inner

            sdk_plugin.NekoPluginBase = NekoPluginBase
            sdk_plugin.Ok = lambda result=None: {{"ok": True, "result": result}}
            sdk_plugin.Err = lambda err=None: {{"ok": False, "error": err}}
            sdk_plugin.SdkError = type("SdkError", (Exception,), {{}})
            for name in ("lifecycle", "llm_tool", "message", "plugin_entry", "neko_plugin"):
                setattr(sdk_plugin, name, _deco)
            plugin_mod.__path__ = []
            plugin_mod.sdk = sdk_mod
            sdk_mod.plugin = sdk_plugin
            sys.modules["plugin"] = plugin_mod
            sys.modules["plugin.sdk"] = sdk_mod
            sys.modules["plugin.sdk.plugin"] = sdk_plugin

            root = r"{ROOT}"
            spec = importlib.util.spec_from_file_location(
                "_nsc_frozen", root + "\\\\__init__.py", submodule_search_locations=[root]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules["_nsc_frozen"] = module
            try:
                spec.loader.exec_module(module)
            except BaseException as exc:
                import traceback
                print("IMPORT_FAILED:", type(exc).__name__, exc)
                traceback.print_exc()
                raise SystemExit(1)
            assert module.StudyCopilotPlugin is not None
            print("IMPORT_OK")
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
        )
        assert "IMPORT_OK" in proc.stdout, f"模拟冻结宿主下 import 失败：\n{proc.stdout}\n{proc.stderr}"

    def test_mini_robots(self):
        crawl = load("_crawl")
        robots = crawl._MiniRobots(
            "User-agent: *\nDisallow: /admin\nDisallow: /private\nAllow: /admin/public\n",
            "Mozilla/5.0",
        )
        assert robots.can_fetch("Mozilla/5.0", "https://x.com/admin/secret") is False
        assert robots.can_fetch("Mozilla/5.0", "https://x.com/admin/public/a") is True
        assert robots.can_fetch("Mozilla/5.0", "https://x.com/course/1") is True

    def test_mini_robots_ignores_other_agents(self):
        crawl = load("_crawl")
        robots = crawl._MiniRobots("User-agent: BadBot\nDisallow: /\n", "Mozilla/5.0")
        assert robots.can_fetch("Mozilla/5.0", "https://x.com/anything") is True

    def test_entity_unescape_fallback(self):
        crawl = load("_crawl")
        assert crawl._unescape_entities("a&amp;b &#65;") == "a&b A"

    def test_robots_loader_never_raises(self):
        crawl = load("_crawl")
        import tempfile
        from pathlib import Path as _Path

        with tempfile.TemporaryDirectory() as tmp:
            crawler = crawl.Crawler(_Path(tmp), interval_ms=0, respect_robots=True, timeout=0.5)
            # 域名不存在 / 无网络时也必须放行，不能抛异常
            assert crawler.robots_allows("https://127.0.0.1:9/never") in (True, False)


def _raise_no_loop():
    raise RuntimeError("no loop")


class TestAsyncBridge:
    """宿主可能把 startup 跑在临时循环里、跑完就关；面板必须照样能用。"""

    def test_uses_live_host_loop(self):
        import asyncio
        import threading

        panel = load("_panel")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self._serve, args=(loop,), daemon=True)
        thread.start()
        bridge = panel.AsyncBridge()
        bridge.bind(loop)
        assert "运行中" in bridge.describe()

        async def work():
            return {"ok": True, "via": "host"}

        try:
            assert bridge.call(work, timeout=10)["via"] == "host"
        finally:
            loop.call_soon_threadsafe(loop.stop)

    def test_falls_back_when_host_loop_closed(self):
        import asyncio

        panel = load("_panel")
        dead = asyncio.new_event_loop()
        dead.close()
        bridge = panel.AsyncBridge()
        bridge.bind(dead)
        assert "已关闭" in bridge.describe()

        async def work():
            return {"ok": True, "via": "private"}

        result = bridge.call(work, timeout=10)
        assert result["via"] == "private"
        assert "运行中" in bridge.describe()

    def test_bind_if_dead_only_replaces_dead_loop(self):
        import asyncio

        panel = load("_panel")
        bridge = panel.AsyncBridge()
        dead = asyncio.new_event_loop()
        dead.close()
        live = asyncio.new_event_loop()
        other = asyncio.new_event_loop()
        try:
            assert bridge.bind_if_dead(live) is True
            assert bridge.bind_if_dead(other) is False  # 还活着就不换
            bridge.bind(dead)
            assert bridge.bind_if_dead(other) is True
        finally:
            for loop in (live, other):
                loop.close()

    def test_call_reports_when_no_loop_available(self):
        import asyncio

        panel = load("_panel")
        bridge = panel.AsyncBridge()
        dead = asyncio.new_event_loop()
        dead.close()
        bridge.bind(dead)
        original = asyncio.new_event_loop
        asyncio.new_event_loop = _raise_no_loop
        try:
            try:
                bridge.call(self._never_called)
            except RuntimeError as exc:
                assert "事件循环不可用" in str(exc)
            else:
                raise AssertionError("没有可用循环时必须报错，不能静默返回")
        finally:
            asyncio.new_event_loop = original

    @staticmethod
    async def _never_called():
        return {}

    @staticmethod
    def _serve(loop):
        import asyncio

        asyncio.set_event_loop(loop)
        loop.run_forever()


class TestPanelServer:
    def test_roundtrip(self):
        panel = load("_panel")
        port = panel.find_open_port(15920)
        server = panel.PanelServer(
            port,
            lambda: "<!doctype html><html>ok</html>",
            {
                ("GET", "/api/status"): lambda _body: {"ok": True, "profile": {"exam_type": "kaoyan"}},
                ("POST", "/api/echo"): lambda body: {"ok": True, "got": body.get("x")},
            },
        )
        assert server.start(), "面板服务应能在本机端口上启动"
        try:
            import json as _json
            import urllib.request

            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                assert b"<html>" in resp.read()
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
                assert _json.loads(resp.read().decode("utf-8"))["profile"]["exam_type"] == "kaoyan"
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/echo",
                data=b'{"x": 1}',
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as resp:
                assert _json.loads(resp.read().decode("utf-8"))["got"] == 1
        finally:
            server.stop()


class TestSources:
    def test_sources_are_free(self):
        sources = load("_sources")
        rows = sources.list_sources()
        assert len(rows) >= 6
        assert all(row["free"] for row in rows)

    def test_query_building(self):
        sources = load("_sources")
        assert sources.build_query("导数", "math", "gaokao") == "gaokao math 导数"
