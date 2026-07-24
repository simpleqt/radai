"""Load subject-specific extraction schemas from YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re
import unicodedata
from typing import Any

import yaml


SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
DEFAULT_SCHEMA_ID = "generic"
SCHEMA_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

# UI 展示名（schema_id → 中文标签）
SCHEMA_LABELS: dict[str, str] = {
    "generic": "通用（跨领域）",
    "basic_mechanics": "基础力学",
    "nuclear_electronics": "核电子学",
    "hydrogeology": "水文地质",
}


@dataclass(frozen=True, slots=True)
class EntityTypeSpec:
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RelationTypeSpec:
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    allowed_source_types: tuple[str, ...] = ()
    allowed_target_types: tuple[str, ...] = ()
    min_confidence: int = 55


@dataclass(frozen=True, slots=True)
class SchemaDefinition:
    schema_id: str
    version: str = "1.0"
    subject: str = DEFAULT_SCHEMA_ID
    label: str | None = None
    entity_types: dict[str, EntityTypeSpec] = field(default_factory=dict)
    relation_types: dict[str, RelationTypeSpec] = field(default_factory=dict)
    forbidden_relations: frozenset[str] = frozenset()
    min_entity_confidence: int = 55
    min_relation_confidence: int = 55
    candidate_confidence: int = 40

    @property
    def schema_version(self) -> str:
        return f"{self.schema_id}_v{self.version}"

    def entity_type_names(self) -> set[str]:
        return set(self.entity_types)

    def relation_type_names(self) -> set[str]:
        return set(self.relation_types)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item).strip() for item in value if str(item).strip())


def _as_string_set(value: Any) -> frozenset[str]:
    return frozenset(_as_tuple(value))


def _canonical_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text.strip()).lower()
    return text


_BUILTIN_ENTITY_ALIASES: dict[str, dict[str, str]] = {
    "generic": {
        "参数": "parameter",
        "术语": "term_cn",
        "概念": "term_cn",
        "型号": "model_or_code",
        "编号": "model_or_code",
        "代码": "model_or_code",
    },
    "hydrogeology": {
        "含水层": "aquifer",
        "隔水层": "aquifer",
        "地层": "aquifer",
        "地层单元": "aquifer",
        "污染物": "contaminant",
        "离子": "contaminant",
        "化学指标": "contaminant",
        "区域": "location",
        "井位": "location",
        "剖面": "location",
        "采样点": "location",
        "地点": "location",
        "位置": "location",
        "参数": "parameter",
        "水位": "parameter",
        "渗透系数": "parameter",
        "浓度": "parameter",
        "流速": "parameter",
        "过程": "process",
        "补给": "process",
        "径流": "process",
        "排泄": "process",
        "迁移": "process",
        "吸附": "process",
        "方法": "method",
        "试验": "method",
        "模型": "method",
        "监测": "method",
        "评价方法": "method",
        "术语": "term_cn",
        "型号": "model_or_code",
        "编号": "model_or_code",
        "代码": "model_or_code",
    },
    "nuclear_electronics": {
        "探测器": "detector",
        "传感器": "detector",
        "测量装置": "detector",
        "设备": "detector",
        "电路": "circuit",
        "模块": "circuit",
        "芯片": "circuit",
        "读出系统": "circuit",
        "组件": "circuit",
        "前置放大器": "circuit",
        "核素": "isotope",
        "放射源": "isotope",
        "粒子": "isotope",
        "参数": "parameter",
        "能量": "parameter",
        "效率": "parameter",
        "分辨率": "parameter",
        "阈值": "parameter",
        "材料": "material",
        "半导体": "material",
        "闪烁体": "material",
        "屏蔽材料": "material",
        "方法": "method",
        "标定": "method",
        "滤波": "method",
        "重建": "method",
        "仿真": "method",
        "术语": "term_cn",
        "型号": "model_or_code",
        "编号": "model_or_code",
        "代码": "model_or_code",
    },
}

_BUILTIN_RELATION_ALIASES: dict[str, dict[str, str]] = {
    "generic": {
        "共现": "co_occurs",
        "共同出现": "co_occurs",
        "相关": "co_occurs",
    },
    "hydrogeology": {
        "位于": "located_in",
        "位于区域": "located_in",
        "所在": "located_in",
        "具有参数": "has_parameter",
        "参数": "has_parameter",
        "影响": "affects",
        "作用于": "affects",
        "测得": "measured_by",
        "测量方法": "measured_by",
        "由方法测得": "measured_by",
        "切割": "intersects",
        "交切": "intersects",
        "穿越": "intersects",
        "共现": "co_occurs",
        "共同出现": "co_occurs",
        "相关": "co_occurs",
    },
    "nuclear_electronics": {
        "测量": "measures",
        "探测": "measures",
        "测量对象": "measures",
        "使用材料": "uses_material",
        "采用材料": "uses_material",
        "使用": "uses_material",
        "具有参数": "has_parameter",
        "参数": "has_parameter",
        "改善": "improves",
        "提升": "improves",
        "提高": "improves",
        "共现": "co_occurs",
        "共同出现": "co_occurs",
        "相关": "co_occurs",
    },
}


def _schema_path(subject: str | None) -> Path:
    normalized = (subject or DEFAULT_SCHEMA_ID).strip().lower() or DEFAULT_SCHEMA_ID
    candidate = SCHEMA_DIR / f"{normalized}.yaml"
    if candidate.exists():
        return candidate
    return SCHEMA_DIR / f"{DEFAULT_SCHEMA_ID}.yaml"


def _schema_file_path(schema_id: str) -> Path:
    return SCHEMA_DIR / f"{normalize_schema_id(schema_id)}.yaml"


def normalize_schema_id(schema_id: str) -> str:
    normalized = (schema_id or "").strip().lower()
    if not SCHEMA_ID_RE.fullmatch(normalized):
        raise ValueError("schema_id 只能包含小写字母、数字、下划线，且必须以字母开头")
    return normalized


def _load_raw_schema(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Schema YAML 顶层必须是对象")
    return raw


def _validate_schema_yaml(raw_yaml: str, expected_schema_id: str | None = None) -> dict[str, Any]:
    raw = yaml.safe_load(raw_yaml) or {}
    if not isinstance(raw, dict):
        raise ValueError("Schema YAML 顶层必须是对象")

    schema_id = normalize_schema_id(str(raw.get("schema_id") or expected_schema_id or ""))
    if expected_schema_id and schema_id != normalize_schema_id(expected_schema_id):
        raise ValueError("YAML 内的 schema_id 必须与当前模板一致")

    entity_types = raw.get("entity_types")
    relation_types = raw.get("relation_types")
    if not isinstance(entity_types, dict) or not entity_types:
        raise ValueError("entity_types 必须是非空对象")
    if not isinstance(relation_types, dict) or not relation_types:
        raise ValueError("relation_types 必须是非空对象")

    raw["schema_id"] = schema_id
    raw["version"] = str(raw.get("version") or "1.0")
    raw["subject"] = str(raw.get("subject") or schema_id)
    raw["forbidden_relations"] = list(_as_tuple(raw.get("forbidden_relations")))
    return raw


def _schema_option_from_path(path: Path) -> dict[str, Any]:
    schema = _load_schema(str(path.resolve()))
    return {
        "schema_id": schema.schema_id,
        "subject": schema.subject,
        "version": schema.version,
        "schema_version": schema.schema_version,
        "label": schema.label or SCHEMA_LABELS.get(schema.schema_id, schema.schema_id),
        "entity_type_count": len(schema.entity_types),
        "relation_type_count": len(schema.relation_types),
    }


def clear_schema_cache() -> None:
    _load_schema.cache_clear()


@lru_cache(maxsize=64)
def _load_schema(path_text: str) -> SchemaDefinition:
    path = Path(path_text)
    raw = _load_raw_schema(path)
    entity_types = {
        name: EntityTypeSpec(
            name=name,
            description=str(spec.get("description") or ""),
            aliases=_as_tuple(spec.get("aliases")),
        )
        for name, spec in (raw.get("entity_types") or {}).items()
        if isinstance(spec, dict)
    }
    relation_types = {
        name: RelationTypeSpec(
            name=name,
            description=str(spec.get("description") or ""),
            aliases=_as_tuple(spec.get("aliases")),
            allowed_source_types=_as_tuple(spec.get("allowed_source_types")),
            allowed_target_types=_as_tuple(spec.get("allowed_target_types")),
            min_confidence=int(spec.get("min_confidence") or raw.get("min_relation_confidence") or 55),
        )
        for name, spec in (raw.get("relation_types") or {}).items()
        if isinstance(spec, dict)
    }
    return SchemaDefinition(
        schema_id=str(raw.get("schema_id") or path.stem),
        version=str(raw.get("version") or "1.0"),
        subject=str(raw.get("subject") or path.stem),
        label=str(raw.get("label") or "") or None,
        entity_types=entity_types,
        relation_types=relation_types,
        forbidden_relations=_as_string_set(raw.get("forbidden_relations")),
        min_entity_confidence=int(raw.get("min_entity_confidence") or 55),
        min_relation_confidence=int(raw.get("min_relation_confidence") or 55),
        candidate_confidence=int(raw.get("candidate_confidence") or 40),
    )


def load_schema_for_subject(subject: str | None) -> SchemaDefinition:
    """Load the best matching subject schema, falling back to generic."""
    return _load_schema(str(_schema_path(subject)))


def _alias_map(schema: SchemaDefinition, *, kind: str) -> dict[str, str]:
    if kind == "entity":
        specs = schema.entity_types
        builtin = _BUILTIN_ENTITY_ALIASES.get(schema.schema_id, {})
    else:
        specs = schema.relation_types
        builtin = _BUILTIN_RELATION_ALIASES.get(schema.schema_id, {})
    aliases = {_canonical_label(name): name for name in specs}
    for alias, target in builtin.items():
        if target in specs:
            aliases[_canonical_label(alias)] = target
    for name, spec in specs.items():
        for alias in getattr(spec, "aliases", ()) or ():
            aliases[_canonical_label(alias)] = name
    return aliases


def canonical_entity_type(schema: SchemaDefinition | None, value: Any) -> str:
    """Map localized or aliased entity type labels to schema ids."""
    raw = str(value or "").strip()
    if not raw or schema is None:
        return raw
    return _alias_map(schema, kind="entity").get(_canonical_label(raw), raw)


def canonical_relation_type(schema: SchemaDefinition | None, value: Any) -> str:
    """Map localized or aliased relation labels to schema ids."""
    raw = str(value or "").strip()
    if not raw or schema is None:
        return raw
    return _alias_map(schema, kind="relation").get(_canonical_label(raw), raw)


def format_schema_for_prompt(schema: SchemaDefinition) -> str:
    """Render a compact but explicit schema contract for extraction prompts."""
    entity_lines = []
    for name, spec in schema.entity_types.items():
        aliases = ", ".join(spec.aliases or ())
        alias_text = f"; aliases: {aliases}" if aliases else ""
        entity_lines.append(f"- {name}: {spec.description}{alias_text}")

    relation_lines = []
    for name, spec in schema.relation_types.items():
        source = ", ".join(spec.allowed_source_types or ("any",))
        target = ", ".join(spec.allowed_target_types or ("any",))
        aliases = ", ".join(spec.aliases or ())
        alias_text = f"; aliases: {aliases}" if aliases else ""
        relation_lines.append(
            f"- {name}: {spec.description}; source_types=[{source}]; "
            f"target_types=[{target}]; min_confidence={spec.min_confidence}{alias_text}"
        )

    return (
        f"schema_version: {schema.schema_version}\n"
        "entity_types:\n" + "\n".join(entity_lines) + "\n"
        "relation_types:\n" + "\n".join(relation_lines) + "\n"
        f"forbidden_relations: {sorted(schema.forbidden_relations)}"
    )


def few_shot_examples_for_schema(schema: SchemaDefinition) -> list[dict[str, Any]]:
    """Small schema-specific examples used only as output-shape anchors."""
    if schema.schema_id == "basic_mechanics":
        return [
            {
                "input": "质量为 2 kg 的物体受到 10 N 水平合力，根据牛顿第二定律，加速度为 5 m/s²。",
                "output": {
                    "entities": [
                        {"name": "牛顿第二定律", "entity_type": "law", "confidence": 95, "chunk_ids": ["示例chunk"]},
                        {"name": "F=ma", "entity_type": "formula", "confidence": 90, "chunk_ids": ["示例chunk"]},
                        {"name": "质量 2 kg", "entity_type": "physical_quantity", "confidence": 90, "chunk_ids": ["示例chunk"]},
                        {"name": "加速度 5 m/s²", "entity_type": "physical_quantity", "confidence": 90, "chunk_ids": ["示例chunk"]},
                    ],
                    "relations": [
                        {"source": "牛顿第二定律", "target": "F=ma", "relation_type": "expressed_by", "confidence": 90, "chunk_ids": ["示例chunk"]},
                        {"source": "F=ma", "target": "加速度 5 m/s²", "relation_type": "solves_for", "confidence": 85, "chunk_ids": ["示例chunk"]},
                    ],
                },
            }
        ]
    if schema.schema_id == "hydrogeology":
        return [
            {
                "input": "承压含水层渗透系数 K=2.5 m/d，断层 F3 切割含水层。",
                "output": {
                    "entities": [
                        {"name": "承压含水层", "entity_type": "aquifer", "confidence": 85, "chunk_ids": ["示例chunk"]},
                        {"name": "渗透系数 K=2.5 m/d", "entity_type": "parameter", "confidence": 90, "chunk_ids": ["示例chunk"]},
                        {"name": "断层 F3", "entity_type": "aquifer", "confidence": 70, "chunk_ids": ["示例chunk"]},
                    ],
                    "relations": [
                        {"source": "承压含水层", "target": "渗透系数 K=2.5 m/d", "relation_type": "has_parameter", "confidence": 85, "chunk_ids": ["示例chunk"]},
                        {"source": "断层 F3", "target": "承压含水层", "relation_type": "intersects", "confidence": 70, "chunk_ids": ["示例chunk"]},
                    ],
                },
            }
        ]
    if schema.schema_id == "nuclear_electronics":
        return [
            {
                "input": "HPGe 探测器使用锗材料，能量分辨率为 1.8 keV。",
                "output": {
                    "entities": [
                        {"name": "HPGe 探测器", "entity_type": "detector", "confidence": 90, "chunk_ids": ["示例chunk"]},
                        {"name": "锗材料", "entity_type": "material", "confidence": 85, "chunk_ids": ["示例chunk"]},
                        {"name": "能量分辨率 1.8 keV", "entity_type": "parameter", "confidence": 85, "chunk_ids": ["示例chunk"]},
                    ],
                    "relations": [
                        {"source": "HPGe 探测器", "target": "锗材料", "relation_type": "uses_material", "confidence": 85, "chunk_ids": ["示例chunk"]},
                        {"source": "HPGe 探测器", "target": "能量分辨率 1.8 keV", "relation_type": "has_parameter", "confidence": 80, "chunk_ids": ["示例chunk"]},
                    ],
                },
            }
        ]
    return [
        {
            "input": "Alpha 与 Beta 在同一段落中共同出现。",
            "output": {
                "entities": [
                    {"name": "Alpha", "entity_type": "term", "confidence": 75, "chunk_ids": ["示例chunk"]},
                    {"name": "Beta", "entity_type": "term", "confidence": 75, "chunk_ids": ["示例chunk"]},
                ],
                "relations": [
                    {"source": "Alpha", "target": "Beta", "relation_type": "co_occurs", "confidence": 55, "chunk_ids": ["示例chunk"]},
                ],
            },
        }
    ]


def list_extraction_schemas() -> list[dict[str, Any]]:
    """List YAML extraction schemas available for self-ingestion prompt profiles."""
    items: list[dict[str, Any]] = []
    for path in sorted(SCHEMA_DIR.glob("*.yaml")):
        items.append(_schema_option_from_path(path))
    return items


def get_extraction_schema(schema_id: str) -> dict[str, Any]:
    """Return one extraction schema with raw YAML for admin editing."""
    path = _schema_file_path(schema_id)
    if not path.exists():
        raise FileNotFoundError(schema_id)
    return {
        **_schema_option_from_path(path),
        "raw_yaml": path.read_text(encoding="utf-8"),
    }


def build_extraction_schema_yaml(
    schema_id: str,
    *,
    label: str,
    subject: str | None = None,
    version: str = "1.0",
) -> str:
    normalized = normalize_schema_id(schema_id)
    raw = {
        "schema_id": normalized,
        "label": label.strip() or normalized,
        "version": str(version or "1.0"),
        "subject": (subject or normalized).strip() or normalized,
        "min_entity_confidence": 55,
        "min_relation_confidence": 55,
        "candidate_confidence": 40,
        "entity_types": {
            "term": {"description": "领域术语、概念或对象"},
            "parameter": {"description": "带数值、单位或状态的参数"},
        },
        "relation_types": {
            "co_occurs": {
                "description": "同一 chunk 中共同出现的弱关联",
                "min_confidence": 50,
            }
        },
        "forbidden_relations": [],
    }
    return yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)


def create_extraction_schema(
    schema_id: str,
    raw_yaml: str,
) -> dict[str, Any]:
    """Create a YAML extraction schema and return its admin detail."""
    normalized = normalize_schema_id(schema_id)
    path = _schema_file_path(normalized)
    if path.exists():
        raise FileExistsError(normalized)
    raw = _validate_schema_yaml(raw_yaml, expected_schema_id=normalized)
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    clear_schema_cache()
    return get_extraction_schema(normalized)


def update_extraction_schema(
    schema_id: str,
    raw_yaml: str,
) -> dict[str, Any]:
    """Update a YAML extraction schema and return its admin detail."""
    normalized = normalize_schema_id(schema_id)
    path = _schema_file_path(normalized)
    if not path.exists():
        raise FileNotFoundError(normalized)
    raw = _validate_schema_yaml(raw_yaml, expected_schema_id=normalized)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    clear_schema_cache()
    return get_extraction_schema(normalized)
