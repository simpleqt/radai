"""学习偏好测评题库、计分与结果解释。"""

from __future__ import annotations

from typing import Any

QUESTIONNAIRE_VERSION = "2026.1"

DIMENSIONS = [
    {
        "id": "processing",
        "title": "信息加工",
        "title_en": "Processing",
        "left": "活跃型",
        "right": "沉思型",
        "description": "你更倾向通过行动与交流，还是通过独立思考来加工信息。",
        "left_summary": "你通常在尝试、讨论和应用中逐渐形成理解。",
        "right_summary": "你通常先在头脑中梳理和分析，再形成稳定理解。",
        "left_tips": ["学完一个概念后立刻做一道练习", "用讲解或讨论检验自己的理解", "把较长学习任务拆成多个行动节点"],
        "right_tips": ["预留安静复盘和独立总结时间", "回答前先写下思路或推导过程", "定期把零散知识整理成自己的框架"],
    },
    {
        "id": "perception",
        "title": "信息感知",
        "title_en": "Perception",
        "left": "感知型",
        "right": "直觉型",
        "description": "你更关注事实、细节与实践，还是概念、规律与可能性。",
        "left_summary": "你通常偏好具体事实、清晰步骤和现实应用。",
        "right_summary": "你通常偏好抽象概念、内在联系和新的可能性。",
        "left_tips": ["从实例、数据和实际场景切入", "明确记录条件、单位和操作步骤", "用真实问题检验抽象理论"],
        "right_tips": ["先追问原理以及概念之间的联系", "尝试提出假设、变式和开放问题", "补充必要细节，避免跳过基础步骤"],
    },
    {
        "id": "input",
        "title": "信息输入",
        "title_en": "Input",
        "left": "视觉型",
        "right": "言语型",
        "description": "你更偏好图像与空间结构，还是文字与语言表达。",
        "left_summary": "图表、结构关系和空间布局通常更容易吸引你的注意。",
        "right_summary": "文字、讲解和语言组织通常更容易帮助你理解。",
        "left_tips": ["把文字材料转成流程图或概念图", "使用颜色和空间位置标记关系", "为公式和过程补充示意图"],
        "right_tips": ["用自己的话复述概念和推理", "阅读后写摘要、定义和关键句", "通过提问、讨论或录音讲解加深理解"],
    },
    {
        "id": "understanding",
        "title": "理解方式",
        "title_en": "Understanding",
        "left": "序列型",
        "right": "综合型",
        "description": "你更倾向逐步构建理解，还是先把握整体再连接细节。",
        "left_summary": "你通常沿着连续、清晰的步骤逐渐建立完整理解。",
        "right_summary": "你通常先形成整体图景，再突然看清细节之间的联系。",
        "left_tips": ["按先修关系安排学习顺序", "为复杂任务列出明确步骤", "每一步都确认理解后再继续"],
        "right_tips": ["开始前先浏览目录、目标和总体结构", "用总览图连接当前细节与整体", "遇到卡点时回到全局问题重新定位"],
    },
]


def _question(question_id: str, dimension: str, text: str, left: str, right: str) -> dict[str, Any]:
    return {
        "id": question_id,
        "dimension": dimension,
        "text": text,
        "options": [
            {"key": "a", "text": left},
            {"key": "b", "text": right},
        ],
    }


QUESTIONS = [
    _question("processing_01", "processing", "当我开始学习一项新技能时，我通常会：", "先动手尝试，在过程中理解", "先了解原理，想清楚后再开始"),
    _question("processing_02", "processing", "面对刚学到的知识，我更愿意：", "马上找例题或任务实践", "先独自回顾并整理思路"),
    _question("processing_03", "processing", "小组学习时，我通常：", "主动表达想法并参与讨论", "先倾听和思考，再选择是否发言"),
    _question("processing_04", "processing", "遇到复杂问题时，我更常：", "边尝试边调整解决办法", "停下来分析各种可能后再行动"),
    _question("processing_05", "processing", "一段讲解结束后，最能帮助我的是：", "立刻完成一个相关操作", "安静地回想讲解的逻辑"),
    _question("processing_06", "processing", "我检验自己是否学会的方法更接近：", "实际做给别人看", "在脑中完整推演一遍"),
    _question("processing_07", "processing", "参加讨论课时，我通常觉得：", "参与互动能让我越来越清楚", "提前思考能让我表达得更准确"),
    _question("processing_08", "processing", "学习软件或仪器时，我更喜欢：", "打开功能逐个试用", "先理解界面和操作逻辑"),
    _question("processing_09", "processing", "收到一个开放任务后，我倾向：", "先做出初稿，再持续修改", "先构思完整方案，再开始制作"),
    _question("processing_10", "processing", "记住一个新概念时，对我更有效的是：", "把它用于一次实际任务", "把它和已有知识认真比较"),
    _question("processing_11", "processing", "课堂上老师提出问题时，我更可能：", "迅速说出初步想法", "想得比较完整后再回答"),
    _question("perception_01", "perception", "学习一个主题时，我更先关注：", "可观察的事实和具体例子", "背后的原理和核心概念"),
    _question("perception_02", "perception", "我更喜欢解决的问题是：", "条件明确、有可验证答案的问题", "需要发现规律或提出新思路的问题"),
    _question("perception_03", "perception", "阅读专业材料时，我更信任：", "数据、案例和实际证据", "理论推导和概念解释"),
    _question("perception_04", "perception", "面对一套操作流程，我更在意：", "每一步是否准确可执行", "流程为什么这样设计"),
    _question("perception_05", "perception", "我更容易对什么内容产生兴趣：", "与现实任务直接相关的内容", "可能带来新发现的抽象问题"),
    _question("perception_06", "perception", "学习公式时，我更希望先看到：", "代入数字的具体算例", "公式的来源和适用规律"),
    _question("perception_07", "perception", "完成实验时，我通常更擅长：", "按要求记录现象和细节", "从现象中猜测更一般的解释"),
    _question("perception_08", "perception", "老师讲解新内容时，我更喜欢：", "定义清楚并配有实际案例", "能够连接多个概念并引出新问题"),
    _question("perception_09", "perception", "我对重复练习的感受更接近：", "它能帮助我熟练掌握方法", "掌握规律后我更想尝试变化"),
    _question("perception_10", "perception", "检查答案时，我更关注：", "计算、单位和条件是否准确", "思路是否具有普遍性和启发性"),
    _question("perception_11", "perception", "选择学习资料时，我更偏好：", "步骤完整、案例丰富的实用资料", "观点新颖、强调原理的理论资料"),
    _question("input_01", "input", "第一次接触复杂系统时，我更希望看到：", "结构图、流程图或界面示意", "文字说明或口头介绍"),
    _question("input_02", "input", "回忆昨天学过的内容时，我更先想起：", "页面、图形或位置", "句子、关键词或讲解声音"),
    _question("input_03", "input", "理解一组数据时，对我更直观的是：", "图表中的趋势和对比", "对数据含义的文字描述"),
    _question("input_04", "input", "记课堂笔记时，我更常使用：", "箭头、框图、颜色和布局", "完整语句、列表和关键词"),
    _question("input_05", "input", "学习设备组成时，我更想先看：", "部件位置和连接示意图", "各部件功能的文字说明"),
    _question("input_06", "input", "别人向我说明路线时，我更喜欢：", "地图或在纸上画出来", "按方向一步步说清楚"),
    _question("input_07", "input", "复习一个章节时，我更愿意：", "制作一张概念图或表格", "写一份摘要或向自己讲述"),
    _question("input_08", "input", "观看教学内容时，我更关注：", "演示画面、动画和标注", "讲解者使用的语言和表达"),
    _question("input_09", "input", "面对抽象关系时，我通常会：", "把它画成图再思考", "把它说成一句话再思考"),
    _question("input_10", "input", "说明自己的方案时，我更自然地使用：", "草图、图表或演示", "文字报告或口头说明"),
    _question("input_11", "input", "查找材料中的重点时，我更容易注意到：", "版式、图形和视觉标记", "标题、术语和关键表述"),
    _question("understanding_01", "understanding", "学习一套新理论时，我更舒服的方式是：", "从基础概念开始逐步推进", "先了解它试图解释的整体问题"),
    _question("understanding_02", "understanding", "阅读长篇材料前，我更倾向：", "从第一部分按顺序阅读", "先浏览目录和结论把握全貌"),
    _question("understanding_03", "understanding", "解决多步骤问题时，我通常：", "完成并确认一步后再做下一步", "先判断最终结构，再补充中间步骤"),
    _question("understanding_04", "understanding", "当暂时不理解某个细节时，我更可能：", "回到前一步查找断点", "继续了解整体，等待关系变清楚"),
    _question("understanding_05", "understanding", "老师讲课的组织方式，我更喜欢：", "内容按难度和依赖关系递进", "先给全局框架，再在各处展开"),
    _question("understanding_06", "understanding", "构建知识体系时，我更常：", "把相邻知识点一个个连接起来", "先形成大图景，再把知识点放进去"),
    _question("understanding_07", "understanding", "面对陌生项目时，我更想先知道：", "第一步具体应该做什么", "项目最终目标和各部分关系"),
    _question("understanding_08", "understanding", "我的理解过程更像：", "持续积累，每一步都比较清晰", "一段时间模糊，随后突然连成整体"),
    _question("understanding_09", "understanding", "复习多章内容时，我更倾向：", "按章节顺序逐一巩固", "寻找贯穿各章的主题和联系"),
    _question("understanding_10", "understanding", "解释复杂内容时，我通常：", "从前提开始按步骤推导", "先说核心结论和整体结构"),
    _question("understanding_11", "understanding", "学习进度被打断后，我更容易通过什么恢复：", "找到上次完成的具体步骤", "重新查看目标和全局框架"),
]

QUESTION_BY_ID = {question["id"]: question for question in QUESTIONS}
DIMENSION_BY_ID = {dimension["id"]: dimension for dimension in DIMENSIONS}


def validate_answers(answers: dict[str, str], *, require_complete: bool = False) -> dict[str, str]:
    """校验答案并返回规范化副本。"""
    normalized: dict[str, str] = {}
    for question_id, option in answers.items():
        if question_id not in QUESTION_BY_ID:
            raise ValueError(f"未知题目: {question_id}")
        normalized_option = str(option).strip().lower()
        if normalized_option not in {"a", "b"}:
            raise ValueError(f"题目 {question_id} 的选项必须为 a 或 b")
        normalized[question_id] = normalized_option

    if require_complete and len(normalized) != len(QUESTIONS):
        raise ValueError(f"请完成全部 {len(QUESTIONS)} 道题后再提交")
    return normalized


def score_answers(answers: dict[str, str]) -> dict[str, Any]:
    """对完整答案计分并生成面向前端的结果。"""
    normalized = validate_answers(answers, require_complete=True)
    scores = {dimension["id"]: 0 for dimension in DIMENSIONS}
    counts = {dimension["id"]: {"a": 0, "b": 0} for dimension in DIMENSIONS}

    for question_id, option in normalized.items():
        dimension_id = QUESTION_BY_ID[question_id]["dimension"]
        counts[dimension_id][option] += 1
        scores[dimension_id] += 1 if option == "a" else -1

    results = []
    for dimension in DIMENSIONS:
        dimension_id = dimension["id"]
        score = scores[dimension_id]
        strength_value = abs(score)
        strength = "balanced" if strength_value <= 3 else "moderate" if strength_value <= 7 else "strong"
        preferred_side = "left" if score > 0 else "right"
        results.append(
            {
                "dimension_id": dimension_id,
                "title": dimension["title"],
                "title_en": dimension["title_en"],
                "left": dimension["left"],
                "right": dimension["right"],
                "score": score,
                "left_count": counts[dimension_id]["a"],
                "right_count": counts[dimension_id]["b"],
                "preferred_side": preferred_side,
                "preferred_label": dimension[preferred_side],
                "strength": strength,
                "summary": dimension[f"{preferred_side}_summary"],
                "tips": dimension[f"{preferred_side}_tips"],
            }
        )

    return {
        "questionnaire_version": QUESTIONNAIRE_VERSION,
        "scores": scores,
        "dimensions": results,
        "disclaimer": "结果反映你当前自述的学习偏好，不代表能力高低，也不意味着只能使用某一种学习方式。",
    }


def get_questionnaire() -> dict[str, Any]:
    return {
        "version": QUESTIONNAIRE_VERSION,
        "question_count": len(QUESTIONS),
        "dimensions": DIMENSIONS,
        "questions": QUESTIONS,
    }
