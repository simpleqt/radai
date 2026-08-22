"""探索配置域:领域/学科/agent 解析、预置主题、预置思维导图加载、超时与详解运行时配置。(拆自 explore/service.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.config.settings import settings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import ast
import json
import os

logger = get_logger(__name__)


_MINDMAP_CACHE_TTL = 3600 * 24  # 24小时


_MAX_MINDMAP_CACHE_SIZE = 200  # 最大缓存条目数


# 默认学科/领域名（用于 Prompt）。可按部署需要在 settings 中覆盖。
DEFAULT_SUBJECT = getattr(settings, "explore_subject", None) or "核电子学"


DEFAULT_DOMAIN = "ne"


EXPLORE_DETAIL_WORKFLOW = "lightweight"


@dataclass(frozen=True)
class ExploreDetailRuntimeConfig:
    """探索详解专用运行时配置，与普通问答的默认工作流隔离。"""

    domain: str
    agent_code: str
    workflow_name: str
    model_name: str


# 前端领域标识 → 学科名映射。不同前端（RadAI / CSH-AI）传入不同 domain 以切换学科与预制题库。
SUBJECT_BY_DOMAIN: dict[str, str] = {
    "ne": DEFAULT_SUBJECT,
    "csh": "场地污染水文地质",
    "fmca": "基础力学",
}
AGENT_CODE_BY_DOMAIN: dict[str, str] = {
    "ne": "ne",
    "csh": "csh",
    "fmca": "fmca",
}


def resolve_domain(domain: str | None) -> str:
    """将 Explore 领域归一化为已注册标识。

    省略值、``None`` 和空白值保持历史默认 ``ne``；显式非空未知值
    不得降级到默认领域，避免在错误的专业语境中生成内容。
    """
    normalized = str(domain or "").strip().lower()
    if not normalized:
        return DEFAULT_DOMAIN
    if normalized not in SUBJECT_BY_DOMAIN:
        supported = ", ".join(sorted(SUBJECT_BY_DOMAIN))
        raise ValueError(
            f"Unsupported explore domain {normalized!r}; supported domains: {supported}"
        )
    return normalized


def resolve_subject(domain: str | None, subject: str | None = None) -> str:
    """根据 domain（或显式 subject）解析学科名。"""
    normalized_domain = resolve_domain(domain)
    if subject and subject.strip():
        return subject.strip()
    return SUBJECT_BY_DOMAIN[normalized_domain]


def resolve_agent_code(domain: str | None) -> str:
    """解析 Explore 详解请求应使用的运行时智能体代码。"""
    normalized_domain = resolve_domain(domain)
    try:
        return AGENT_CODE_BY_DOMAIN[normalized_domain]
    except KeyError as exc:  # 新领域必须同时声明运行时智能体。
        raise ValueError(
            f"Explore domain {normalized_domain!r} has no registered runtime agent"
        ) from exc


# 发现态预制主题清单（按学科）。即时返回，避免 LLM 生成带来的等待与超时。
PRESET_TOPICS: dict[str, list[dict[str, str]]] = {
    "基础力学": [
        {"title": "质点运动学", "hint": "位置、速度与加速度"},
        {"title": "牛顿运动定律", "hint": "受力分析与运动方程"},
        {"title": "常见力与约束", "hint": "重力、弹力、摩擦与张力"},
        {"title": "功与动能", "hint": "功—能定理及其应用"},
        {"title": "势能与机械能", "hint": "保守力与能量守恒"},
        {"title": "动量与冲量", "hint": "动量定理和碰撞问题"},
        {"title": "质心运动", "hint": "质点系的整体描述"},
        {"title": "角动量", "hint": "角动量定理与守恒"},
        {"title": "圆周运动", "hint": "切向与法向运动分析"},
        {"title": "万有引力", "hint": "引力场与轨道运动"},
        {"title": "刚体定轴转动", "hint": "角速度、角加速度与转矩"},
        {"title": "转动惯量", "hint": "计算方法与平行轴定理"},
        {"title": "刚体的功和能", "hint": "转动动能与能量方法"},
        {"title": "静力学平衡", "hint": "力和力矩的平衡条件"},
        {"title": "简谐振动", "hint": "运动方程、周期与能量"},
        {"title": "阻尼与受迫振动", "hint": "衰减、共振与稳态响应"},
        {"title": "机械波基础", "hint": "波动方程与能量传播"},
        {"title": "参考系与相对运动", "hint": "速度和加速度变换"},
        {"title": "非惯性系", "hint": "惯性力与转动参考系"},
        {"title": "力学建模方法", "hint": "受力图、约束与量纲检查"},
    ],
    "核电子学": [
        {"title": "核电子学基础", "hint": "信号链与测量系统总览"},
        {"title": "前置放大器", "hint": "微弱信号放大与阻抗匹配"},
        {"title": "主放大器", "hint": "信号放大与脉冲成形"},
        {"title": "脉冲成形电路", "hint": "CR-RC 成形与信噪比优化"},
        {"title": "多道分析器", "hint": "能谱测量与道址分配"},
        {"title": "甄别器与定时", "hint": "阈值甄别与时间测量"},
        {"title": "符合与反符合", "hint": "事件关联与本底抑制"},
        {"title": "模数转换", "hint": "ADC 原理与精度指标"},
        {"title": "数字脉冲处理", "hint": "数字成形与在线算法"},
        {"title": "探测器原理", "hint": "气体/闪烁/半导体探测器"},
        {"title": "闪烁探测器", "hint": "光电倍增与能量分辨"},
        {"title": "半导体探测器", "hint": "HPGe 与硅探测器"},
        {"title": "核辐射测量系统", "hint": "完整测量链路构成"},
        {"title": "能量分辨率", "hint": "影响因素与改善方法"},
        {"title": "能谱分析方法", "hint": "峰位标定与定量分析"},
        {"title": "符合定时技术", "hint": "时间分辨与定时方法"},
        {"title": "辐射剂量测量", "hint": "剂量率与累计剂量"},
        {"title": "辐射防护", "hint": "屏蔽、距离与时间"},
        {"title": "电荷灵敏放大", "hint": "电荷积分与噪声"},
        {"title": "噪声与信噪比", "hint": "噪声来源与抑制"},
        {"title": "中子探测技术", "hint": "慢化、转换与计数"},
        {"title": "时间幅度变换", "hint": "TAC 原理与应用"},
        {"title": "数据获取系统", "hint": "DAQ 架构与触发"},
        {"title": "高压电源", "hint": "探测器偏压与稳定性"},
        {"title": "标准核仪器", "hint": "NIM 机箱与模块"},
        {"title": "信号传输与匹配", "hint": "电缆、阻抗与反射"},
        {"title": "符合测量实验", "hint": "典型实验方法与流程"},
        {"title": "峰康比与本底", "hint": "谱线质量评价指标"},
    ],
    "场地污染水文地质": [
        {"title": "水文地质基础", "hint": "地下水系统总体认识"},
        {"title": "含水层与隔水层", "hint": "类型、特征与判别"},
        {"title": "地下水流动", "hint": "达西定律与流场"},
        {"title": "渗透系数", "hint": "测定方法与影响因素"},
        {"title": "包气带特征", "hint": "非饱和带水分运移"},
        {"title": "抽水试验", "hint": "参数求取与分析"},
        {"title": "地下水补给与排泄", "hint": "水均衡要素"},
        {"title": "污染物运移", "hint": "对流-弥散过程"},
        {"title": "吸附与降解", "hint": "迁移转化机理"},
        {"title": "污染羽演化", "hint": "范围预测与控制"},
        {"title": "污染源识别", "hint": "源解析与示踪"},
        {"title": "场地环境调查", "hint": "分阶段调查流程"},
        {"title": "采样与监测", "hint": "布点、采样与质控"},
        {"title": "地下水监测井", "hint": "建井与洗井规范"},
        {"title": "水文地质勘察", "hint": "勘探手段与编录"},
        {"title": "水质分析", "hint": "常规指标与解读"},
        {"title": "重金属污染", "hint": "形态与迁移特征"},
        {"title": "有机污染物", "hint": "VOCs/LNAPL/DNAPL"},
        {"title": "土壤污染", "hint": "成因与分布规律"},
        {"title": "风险评估", "hint": "暴露途径与管控值"},
        {"title": "概念模型", "hint": "水文地质概念建模"},
        {"title": "数值模拟", "hint": "MODFLOW/MT3D 应用"},
        {"title": "抽出处理", "hint": "P&T 修复技术"},
        {"title": "原位化学氧化", "hint": "ISCO 药剂与工艺"},
        {"title": "可渗透反应墙", "hint": "PRB 设计要点"},
        {"title": "监测自然衰减", "hint": "MNA 评估方法"},
        {"title": "阻隔与帷幕", "hint": "防渗与污染阻隔"},
        {"title": "修复效果评估", "hint": "验收与长期监测"},
    ],
}


def _load_preset_maps() -> dict[str, dict[str, Any]]:
    """加载离线生成的脑图，兼容 JSON 文件和历史 Python 数据文件。"""
    base_dir = Path(__file__).resolve().parent
    preset_maps: dict[str, dict[str, Any]] = {}

    for path in (base_dir / "preset_maps.py", base_dir / "preset_maps.json"):
        if not path.exists():
            continue
        try:
            payload = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                marker = "PRESET_MAPS ="
                if marker not in payload:
                    continue
                payload = payload.split(marker, 1)[1].strip()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = ast.literal_eval(payload)
            if isinstance(parsed, dict):
                preset_maps.update({str(key): value for key, value in parsed.items() if isinstance(value, dict)})
        except (OSError, SyntaxError, ValueError) as exc:
            logger.warning("[Explore] 预制脑图加载失败 (%s): %s", path.name, exc)

    return preset_maps


_PRESET_MAPS = _load_preset_maps()


# 各能力的 LLM 超时（秒）
TIMEOUT_MAP = 25.0


TIMEOUT_EXPAND = 20.0


TIMEOUT_QUIZ = float(os.environ.get("EXPLORE_QUIZ_MODEL_TIMEOUT", "25") or 25)


TIMEOUT_QUIZ_RETRIEVE = float(os.environ.get("EXPLORE_QUIZ_RETRIEVE_TIMEOUT", "2") or 2)


TIMEOUT_GRADE = 25.0


TIMEOUT_DETAIL = 75.0


QUIZ_CONTEXT_CHUNK_TOP_K = int(os.environ.get("EXPLORE_QUIZ_CONTEXT_CHUNK_TOP_K", "4") or 4)


QUIZ_CONTEXT_MAX_CHARS = int(os.environ.get("EXPLORE_QUIZ_CONTEXT_MAX_CHARS", "6000") or 6000)


DEFAULT_LEARNING_FAST_MODEL_NAME = (
    os.environ.get("EXPLORE_FAST_MODEL_NAME")
    or os.environ.get("CUSTOM_LEARNING_FAST_MODEL_NAME")
    or os.environ.get("EXPLORE_EXPAND_MODEL_NAME")
    or "qwen3.6-35b"
).strip() or "qwen3.6-35b"


def resolve_learning_fast_model_name(model_name: str | None = None) -> str:
    return str(model_name or "").strip() or DEFAULT_LEARNING_FAST_MODEL_NAME


def resolve_detail_runtime_config(
    domain: str | None,
    model_name: str | None = None,
) -> ExploreDetailRuntimeConfig:
    """解析探索详解专用配置。

    模型优先级为请求显式值、领域级配置、探索详解公共配置、历史快速模型配置。
    领域级环境变量格式为 ``EXPLORE_DETAIL_<DOMAIN>_MODEL_NAME``，例如
    ``EXPLORE_DETAIL_CSH_MODEL_NAME``。工作流固定为 lightweight，避免普通问答
    的默认 full 配置影响探索详解。
    """
    normalized_domain = resolve_domain(domain)
    domain_model_env = f"EXPLORE_DETAIL_{normalized_domain.upper()}_MODEL_NAME"
    resolved_model = (
        str(model_name or "").strip()
        or str(os.environ.get(domain_model_env) or "").strip()
        or str(os.environ.get("EXPLORE_DETAIL_MODEL_NAME") or "").strip()
        or DEFAULT_LEARNING_FAST_MODEL_NAME
    )
    return ExploreDetailRuntimeConfig(
        domain=normalized_domain,
        agent_code=resolve_agent_code(normalized_domain),
        workflow_name=EXPLORE_DETAIL_WORKFLOW,
        model_name=resolved_model,
    )
