"""智能体-知识库绑定初始化脚本

用法：
    python -m backend.db.init_agent_bindings
    python -m backend.db.init_agent_bindings --force   # 强制重建（清空已有绑定再写入）

该脚本向数据库写入两个智能体的默认配置及知识库绑定：
  - ne_full_feature  : 核电子学智能体
  - csh_full_feature : 成理场地污染水文地质智能体（CSH-AI）
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
        "kb_ids_env_key": "NE_KB_IDS",  # 从环境变量读取，逗号分隔
    },
    {
        "agent_code": "csh_full_feature",
        "agent_name": "CSH-AI 水文地质智能体",
        "description": "专注污染场地水文地质领域，支持知识库检索、深度研究及多轮对话。",
        "is_active": True,
        "sort_order": 1,
        "kb_ids_env_key": "CSH_KB_IDS",  # 从环境变量读取，逗号分隔
    },
]


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

async def init_agent_bindings(force: bool = False) -> None:
    import os

    from backend.db.connection import get_async_session_factory
    from backend.db.crud.agent_crud import (
        get_agent_kb_ids,
        set_agent_kb_bindings,
        upsert_agent,
    )

    session_factory = get_async_session_factory()
    async with session_factory() as session:
        for cfg in _DEFAULT_AGENTS:
            agent_code: str = cfg["agent_code"]

            # 从环境变量读取知识库 ID 列表
            env_key = cfg.get("kb_ids_env_key", "")
            kb_ids_raw = os.getenv(env_key, "") if env_key else ""
            kb_ids: list[str] = [x.strip() for x in kb_ids_raw.split(",") if x.strip()]

            if not kb_ids:
                logger.warning(
                    "[init_agent_bindings] 环境变量 %s 未配置或为空，跳过 %s",
                    env_key,
                    agent_code,
                )
                continue

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
