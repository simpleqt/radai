"""智能体-知识库绑定初始化脚本

用法：
    python -m backend.db.init_agent_bindings
    python -m backend.db.init_agent_bindings --force   # 强制重建（清空已有绑定再写入）

该脚本向数据库写入智能体的默认配置及知识库绑定：
  - ne_full_feature  : 核电子学智能体
  - csh_full_feature : 成理场地污染水文地质智能体（CSH-AI）
  - groundwater      : 地下水项目分析助手（六塘项目标准知识库）
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

_DEFAULT_AGENTS: list[dict] = [
    {
        "agent_code": "ne_full_feature",
        "agent_name": "核电子学智能体",
        "description": "专注核电子学领域，支持双知识库并行检索、深度研究及多轮对话。",
        "is_active": True,
        "sort_order": 0,
        "kb_codes": [
            "rag_nuclear_electronics",
            "NuclearElectronicsPaperRepository",
        ],
    },
    {
        "agent_code": "csh_full_feature",
        "agent_name": "CSH-AI 水文地质智能体",
        "description": "专注污染场地水文地质领域，支持知识库检索、深度研究及多轮对话。",
        "is_active": True,
        "sort_order": 1,
        "kb_codes": ["polluted_site_remediation"],
    },
    {
        "agent_code": "groundwater",
        "agent_name": "地下水项目分析助手",
        "description": "面向授权地下水项目数据与六塘项目标准知识库的分析助手。",
        "is_active": True,
        "sort_order": 2,
        "kb_codes": ["liutang"],
    },
]


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

async def init_agent_bindings(force: bool = False) -> None:
    from backend.db.connection import get_async_session_factory
    from backend.db.crud.agent_crud import (
        get_agent_kb_ids,
        set_agent_kb_bindings,
        upsert_agent,
    )
    from backend.db.models.knowledge_base import KnowledgeBase
    from sqlalchemy import select

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        for cfg in _DEFAULT_AGENTS:
            agent_code: str = cfg["agent_code"]
            kb_codes: list[str] = cfg["kb_codes"]
            rows = (
                await session.execute(
                    select(KnowledgeBase.kb_id, KnowledgeBase.kb_code).where(
                        KnowledgeBase.kb_code.in_(kb_codes)
                    )
                )
            ).all()
            ids_by_code = {str(row.kb_code): str(row.kb_id) for row in rows}
            missing_codes = [code for code in kb_codes if code not in ids_by_code]
            if missing_codes:
                raise RuntimeError(
                    f"MySQL 中不存在初始化所需知识库编码: {', '.join(missing_codes)}"
                )
            kb_ids = [ids_by_code[code] for code in kb_codes]

            # 创建或更新智能体记录
            await upsert_agent(
                session,
                agent_code=agent_code,
                agent_name=cfg["agent_name"],
                description=cfg.get("description"),
                is_active=cfg.get("is_active", True),
                sort_order=cfg.get("sort_order", 0),
            )

            # 检查是否已有绑定
            existing = await get_agent_kb_ids(session, agent_code)
            if existing and not force:
                logger.info(
                    "[init_agent_bindings] 智能体 %s 已存在绑定 %s，跳过（使用 --force 覆盖）",
                    agent_code,
                    existing,
                )
                continue

            await set_agent_kb_bindings(session, agent_code, kb_ids)
            logger.info(
                "[init_agent_bindings] 智能体 %s 绑定知识库: %s",
                agent_code,
                kb_ids,
            )

        await session.commit()
        logger.info("[init_agent_bindings] 初始化完成")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    import os
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # 确保项目根目录在 PYTHONPATH 中
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    parser = argparse.ArgumentParser(description="初始化智能体-知识库绑定")
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重建：清空已有绑定后写入默认值",
    )
    args = parser.parse_args()

    asyncio.run(init_agent_bindings(force=args.force))
    print("✓ 智能体-知识库绑定初始化完成")


if __name__ == "__main__":
    main()
