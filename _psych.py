"""心理暗示与疏导。

原则：

- **不做医疗诊断**。这里做的是学习场景下的情绪调节与行为推动，不是心理治疗。
  出现持续失眠、强烈无望感、自伤念头等信号时，明确建议去找专业人士，并给出真实热线。
- **暗示要给具体动作**，不说「你可以的」这种空话。有效的暗示是：
  「今天只做这三道题，做完就算赢」——可控、可验证、能产生正反馈。
- 按考试类型区分压力源：高考是长跑倦怠，四六级是拖延性焦虑，考研是孤立无援感。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 风险信号：命中就必须停止"打鸡血"，转为明确的求助建议 ──────────
RISK_KEYWORDS = (
    "不想活", "活不下去", "自杀", "自残", "轻生", "结束生命", "死了算了",
    "割腕", "跳楼", "每天都想哭", "整夜睡不着", "连续失眠", "崩溃到",
)
RISK_ADVICE = (
    "这里要停一下：我不适合处理这种情况，也不该假装能处理。\n"
    "如果你已经持续两周以上睡不好、吃不下，或者有伤害自己的念头，请立刻告诉身边信任的人，"
    "并尽快联系专业帮助：\n"
    "  · 12355 青少年服务台（24 小时，免费）\n"
    "  · 400-161-9995 希望 24 热线（24 小时）\n"
    "  · 学校心理咨询中心 / 正规医院临床心理科\n"
    "这不是矫情，是身体在报警，该找专业的人处理。学习的事可以先放一放，先把人照顾好。"
)

EXAM_PRESSURE: dict[str, dict[str, str]] = {
    "gaokao": {
        "label": "高考",
        "source": "长跑型压力：家庭期待 + 同辈竞争 + 一年以上的消耗，最常见的不是紧张，是倦怠和意义感流失",
        "core": "把『我要考个好大学』拆成『这周把这三个专题啃下来』——高考的痛苦大多来自目标太远、反馈太慢",
    },
    "zhongkao": {
        "label": "中考",
        "source": "年纪小、自控力还在长，压力往往来自家长和老师的期待而不是自己",
        "core": "不靠意志力，靠环境设计：手机放远、桌面只留一科、时段固定，让『开始学』这件事不需要做决定",
    },
    "cet4": {
        "label": "英语四级",
        "source": "拖延型焦虑：『裸考也能过』的文化 + 没有过程反馈，于是拖到考前一周才开始慌",
        "core": "四级是短期可突击的考试：每天 40 分钟真题听力 + 阅读，21 天足够把分数拉起来，关键在启动不在时长",
    },
    "cet6": {
        "label": "英语六级",
        "source": "挫折型压力：很多人考了两三次都没过，会形成『我英语就是不行』的固定归因",
        "core": "六级不过通常不是能力问题，是词汇量和时间分配问题——这两样都是可测量的，把归因换到可控项上",
    },
    "kaoyan": {
        "label": "考研",
        "source": "孤立型压力：没有其他人在考同一张卷子，没有模拟考排名，长期没有外部反馈",
        "core": "给自己造反馈：每周固定一套卷子做量化记录；找一个同伴哪怕只是互相报进度，孤独感会显著下降",
    },
    "final": {
        "label": "校内考试",
        "source": "短期突击型压力：时间集中、科目多，容易陷入『什么都想抓结果什么都没抓』",
        "core": "按学分和通过难度排序，先保必修和挂科风险高的科目，其余只求及格线以上",
    },
    "university": {
        "label": "大学期末 / 绩点",
        "source": "「及格就行」和「绩点要好看」是两套完全不同的压力：前者是恐惧，后者是长期比较，"
                 "而且大学没人盯着你学，进度全靠自己发现",
        "core": "先按学分 × 挂科风险保住及格（这是底线），再向高学分科目要绩点；"
                "大学的边际收益很集中——把 3 门 4 学分的课从 75 提到 85，比把 10 门课都提 2 分有用得多",
    },
    "cert_teacher": {
        "label": "教师资格证",
        "source": "自我怀疑型压力：知识点杂而不深，很多人卡的不是难度，是『没写过教案、没练过试讲』",
        "core": "笔试里写作和教学设计占一半分值，是有模板可套的；把力气放在这两块，回报比刷选择题高得多",
    },
    "cert_soft": {
        "label": "软考",
        "source": "在职型压力：白天上班晚上备考，且必须两科同一次都过，一科没过就全部作废",
        "core": "上午题靠刷真题找覆盖面，下午案例（或论文）靠提前准备素材；论文一定要在考前写完两篇完整稿",
    },
    "cert_cpa": {
        "label": "注册会计师",
        "source": "长周期消耗型压力：六科要在五年内过完，单科通过率常年只有两成左右，"
                 "很容易形成『考了两三年一科都没过』的挫败",
        "core": "把它当成项目而不是考试：固定每年报 2-3 科，按关联度搭配（会计+税法、会计+审计），"
                "允许有科目没过，但不允许中断节奏",
    },
    "cert_law": {
        "label": "法律职业资格考试",
        "source": "体量型压力：十几门法、上万条考点，且有『客观题没过就白学一年』的门槛结构",
        "core": "客观题是纯概率游戏，靠刷题量堆覆盖率；主观题才考表达，集中在考前两个月练写作就够了",
    },
    "ielts": {
        "label": "雅思",
        "source": "单项瓶颈型压力：总分被口语或写作卡住，而这两项恰恰最难自己练",
        "core": "口语和写作有明确的评分维度（流利度、词汇、语法、发音 / 任务回应、连贯、词汇、语法），"
                "对着维度逐条补，比无差别练题有效得多",
    },
    "toefl": {
        "label": "托福",
        "source": "时间压迫型压力：机考连做三小时，听力贯穿口语与写作，体力与注意力也是考试的一部分",
        "core": "听力是地基，先解决听不懂的问题；再做全真模考练三小时的持续专注",
    },
    "ncse": {
        "label": "全国计算机等级考试",
        "source": "轻视型压力：以为『很简单』结果卡在上机操作上，操作题没练过基本过不了",
        "core": "选择题背题库足够，操作题必须真的上机做三套以上——眼睛会了手不一定会",
    },
}

MOOD_STRATEGIES: dict[str, dict[str, Any]] = {
    "anxious": {
        "label": "焦虑 / 紧张",
        "reframe": "心跳加快、手心出汗和『兴奋』在生理上几乎一样。告诉自己『我的身体在给我供能』，"
                   "比命令自己『别紧张』有效得多——压抑情绪会占用做题的工作记忆。",
        "actions": [
            "用 4-7-8 呼吸法做三轮（吸 4 秒、屏 7 秒、呼 8 秒），先让身体降档",
            "把焦虑写下来：具体在怕哪一件事？写出来的瞬间它会从『弥漫的恐惧』变成『一个可以处理的问题』",
            "立刻做一道会做的简单题，用一次确定的正反馈打断反刍",
        ],
    },
    "tired": {
        "label": "疲惫 / 学不动",
        "reframe": "疲惫通常不是意志力不够，是恢复不够。继续硬撑会让效率降到原来的三成以下，"
                   "不如明确地休息——休息不是放弃，是把效率买回来。",
        "actions": [
            "今天只做两件事：复习已会的（巩固）+ 一道新题（保持推进感），其余时间用来恢复",
            "睡够 7 小时，效率提升远大于熬夜多出来的 2 小时",
            "检查是不是一直在做最难的那部分——把简单题穿插进去，让进度条动起来",
        ],
    },
    "plateau": {
        "label": "瓶颈 / 分数不涨",
        "reframe": "平台期几乎必然出现，而且它常常出现在真正提升之前：前面学的是『看得懂』，"
                   "现在卡住的是『做得出』。前者是输入，后者是输出，中间隔着刻意练习。",
        "actions": [
            "停止刷新题，改为把错过的题重做三遍——平台期多半是同一个坑反复掉",
            "给错题写一句话错因，分类统计，你会发现 80% 的失分集中在两三个点上",
            "限时做：同样一道题，不限时会做、限时做不出来，这是熟练度问题不是能力问题",
        ],
    },
    "panic": {
        "label": "考前恐慌",
        "reframe": "考前觉得『什么都不会』是正常现象，叫检索失败——不是真的忘了，"
                   "是压力下提取路径被堵住了。上了考场看到题目，提取线索会自己回来。",
        "actions": [
            "考前三天不再开新专题，只做错题和已经会的内容，保护信心",
            "准备一份『进考场前 5 分钟看一眼』的清单：常忘的公式、易错条件、时间分配",
            "按考试时间段作息，让身体在该兴奋的时候兴奋",
        ],
    },
    "procrastinate": {
        "label": "拖延 / 启动不了",
        "reframe": "拖延的情绪解法是『先做 5 分钟再说』——行动会反过来改变情绪，不是等有状态了才开始。",
        "actions": [
            "把任务切到 5 分钟能启动的粒度：不是『复习导数』，是『把昨天的错题重做一遍』",
            "用 if-then 计划：如果到 20:00 还没开始，那我就先只做第一道题",
            "移除启动阻力：前一晚把书和卷子摊开放在桌上，早上坐下就能开始",
        ],
    },
    "lost": {
        "label": "迷茫 / 不知道为什么学",
        "reframe": "不知道为什么而学的时候，靠意志力撑不了多久。先找到哪怕一个具体的、属于自己的理由，"
                   "比任何激励都管用。",
        "actions": [
            "写下一个具体的目标场景（去哪个城市、学什么专业、过什么样的生活），越具体越有牵引力",
            "把目标和今天的任务连起来：今天这三道题，是在为那个场景里的哪一步铺路",
            "允许自己有不想学的时刻，但不允许自己因为不想学就把目标也一起否定掉",
        ],
    },
    "down": {
        "label": "低落 / 考砸了",
        "reframe": "一次成绩说明的只是『这个阶段的方法效果不好』，不是『你不行』。"
                   "把结论落在方法上，你才有下一步可以改；落在人上，就只剩自责。",
        "actions": [
            "给这次考试做一个 10 分钟的归因清单：时间不够 / 知识点不会 / 会但做错，各占多少",
            "只允许难过半天，然后必须挑出一条可执行的改动写进计划",
            "把这次分数当成一次免费的全真诊断——它能告诉你哪里该改，比任何模拟都准",
        ],
    },
}

HINTS: dict[str, str] = {
    "gaokao": "『我不是要考满分，我是要把该拿的分拿稳。』——把注意力从总分拉回到这一道能做对的题上。",
    "zhongkao": "『只要坐下打开书，最难的一步就已经过去了。』",
    "cet4": "『今天只做一篇真题听力。做完就是赢。』",
    "cet6": "『我卡在词汇和时间分配上，这两样都能练。』",
    "kaoyan": "『我不需要状态好，我只需要今天完成这一项。』",
    "final": "『先保住不挂科的科目，再谈高分。』",
    "university": "『及格只是底线，绩点是我给自己攒的选项。今天这一门，值得多要 10 分。』",
    "cert_teacher": "『教学设计有模板，写作有框架——我不是在拼天赋，我是在拼熟练度。』",
    "cert_soft": "『上午靠覆盖，下午靠素材。素材是提前准备的，不是考场上想出来的。』",
    "cert_cpa": "『这是五年的项目，不是一次考试。今年的两科，过一科也是推进。』",
    "cert_law": "『客观题靠覆盖率，我每多刷一百道题，就多覆盖一点。』",
    "ielts": "『卡住我的不是英语，是口语和写作这两个可以拆开练的单项。』",
    "toefl": "『听力是地基，地基打好了，口语和综合写作会一起涨。』",
    "ncse": "『操作题是练出来的，不是看出来的。今天上机做一套，比看十页书有用。』",
}

LEVEL_TONE: dict[str, str] = {
    "light": "点到为止，一句话足够，不要长篇大论。",
    "normal": "给一段有温度但不啰嗦的疏导，先接住情绪，再给 2-3 条具体动作。",
    "deep": "认真聊一聊：先复述他的处境让他觉得被理解，再拆解压力来源，最后给一份可执行的本周安排。",
}


@dataclass
class CounselResult:
    exam_type: str
    exam_label: str
    days_left: int
    stage: str
    mood: str
    mood_label: str
    reading: str
    reframe: str
    actions: list[str] = field(default_factory=list)
    hint: str = ""
    risk: bool = False
    risk_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "exam_type": self.exam_type,
            "exam_label": self.exam_label,
            "days_left": self.days_left,
            "stage": self.stage,
            "mood": self.mood,
            "mood_label": self.mood_label,
            "reading": self.reading,
            "reframe": self.reframe,
            "actions": self.actions,
            "hint": self.hint,
            "risk": self.risk,
            "risk_text": self.risk_text,
        }


MOOD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "anxious": ("焦虑", "紧张", "慌", "压力", "害怕", "担心", "心慌", "忐忑"),
    "tired": ("累", "疲惫", "学不动", "没劲", "困", "撑不住", "想休息"),
    "plateau": ("瓶颈", "不涨", "没进步", "卡住", "提不上", "原地"),
    "panic": ("考前", "快考试了", "来不及", "什么都不会", "完了", "要考了"),
    "procrastinate": ("拖延", "不想学", "学不进去", "摆烂", "躺平", "启动不了", "刷手机"),
    "lost": ("迷茫", "没动力", "为什么学", "没意义", "不知道"),
    "down": ("考砸", "退步", "难过", "失落", "没考好", "崩了", "自责"),
}


def detect_mood(text: str) -> str:
    text = text or ""
    best = "anxious"
    best_hits = 0
    for mood, keywords in MOOD_KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in text)
        if hits > best_hits:
            best, best_hits = mood, hits
    return best


def detect_risk(text: str) -> bool:
    text = text or ""
    return any(keyword in text for keyword in RISK_KEYWORDS)


def stage_of(days_left: int) -> str:
    if days_left <= 0:
        return "考后"
    if days_left <= 7:
        return "临考一周"
    if days_left <= 30:
        return "冲刺月"
    if days_left <= 90:
        return "强化期"
    if days_left <= 200:
        return "基础期"
    return "长线准备期"


def _default_mood(days_left: int, mastery: float) -> str:
    if days_left <= 7:
        return "panic"
    if mastery < 0.4 and days_left > 30:
        return "lost"
    if days_left <= 30:
        return "anxious"
    return "procrastinate"


def counsel(
    exam_type: str,
    days_left: int,
    mastery: float = 0.5,
    mood: str = "",
    text: str = "",
    level: str = "normal",
) -> CounselResult:
    pressure = EXAM_PRESSURE.get(exam_type, EXAM_PRESSURE["gaokao"])
    detected = mood or (detect_mood(text) if text else "")
    if not detected or detected not in MOOD_STRATEGIES:
        detected = _default_mood(days_left, mastery)
    strategy = MOOD_STRATEGIES[detected]
    risk = detect_risk(text)

    reading = (
        f"你现在的处境：{pressure['label']}，{stage_of(days_left)}，还剩 {days_left} 天，"
        f"整体掌握度约 {mastery:.0%}。{pressure['source']}。"
    )
    return CounselResult(
        exam_type=exam_type,
        exam_label=pressure["label"],
        days_left=days_left,
        stage=stage_of(days_left),
        mood=detected,
        mood_label=strategy["label"],
        reading=reading,
        reframe=strategy["reframe"] + "\n" + pressure["core"],
        actions=list(strategy["actions"]),
        hint=HINTS.get(exam_type, ""),
        risk=risk,
        risk_text=RISK_ADVICE if risk else "",
    )


def build_comfort_prompt(result: CounselResult, catgirl_name: str, level: str = "normal") -> tuple[str, str]:
    system = (
        f"你是陪伴备考的猫娘{catgirl_name}。现在要做的是心理疏导，语气要求：{LEVEL_TONE.get(level, LEVEL_TONE['normal'])}\n"
        "硬性要求：\n"
        "  · 不编造励志故事，不喊口号，不说『你一定可以的』这种空话；\n"
        "  · 所有建议必须是今天就能做的具体动作；\n"
        "  · 先接住情绪（承认这个处境确实难），再给方法，顺序不能反；\n"
        "  · 不做医疗诊断、不开药、不替代专业心理治疗；\n"
        "  · 如果用户表达出自我伤害的意思，不要试图自己处理，直接劝其联系专业热线与身边信任的人。\n"
        "用你自己的口吻说，不要复述条目。"
    )
    user = (
        f"{result.reading}\n"
        f"情绪状态：{result.mood_label}\n"
        f"认知重构要点：{result.reframe}\n"
        f"可给的具体动作：\n" + "\n".join(f"  - {item}" for item in result.actions) + "\n"
        f"可用的心理暗示句：{result.hint}\n"
    )
    if result.risk:
        user += f"\n⚠ 检测到风险信号，必须立刻转为求助建议：\n{result.risk_text}\n"
    return system, user


def format_counsel(result: CounselResult) -> str:
    lines = [result.reading, f"\n状态判断：{result.mood_label}", f"\n{result.reframe}", "\n可以现在就做："]
    lines.extend(f"  · {item}" for item in result.actions)
    if result.hint:
        lines.append(f"\n记住这一句：{result.hint}")
    if result.risk:
        lines.append(f"\n{result.risk_text}")
    return "\n".join(lines)
