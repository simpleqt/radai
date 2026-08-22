"""自定义学习路径域:路径监控、低分调整与探索桥接。(拆自 services/custom_learning_agents.py)"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4
from backend.services.custom_learning_planning import (
    LearningNodeBlueprint,
)


class PathMonitorAgent:
    """Defines path-level monitoring and adjustment policy."""

    name = "path_monitor"

    def trace(self, nodes: list[LearningNodeBlueprint], *, duration_days: int) -> dict[str, Any]:
        return {
            "agent": self.name,
            "decision": "monitor_mastery_and_adjust_path",
            "reason": "练习得分低于阈值时需要即时补强，避免带着薄弱点进入后续节点",
            "policy": "score_below_70_insert_review_node",
            "node_count": len(nodes),
            "duration_days": duration_days,
            "confidence": 0.84,
            "impact": "低分后自动插入复习节点，并把当前节点标记为需复习",
        }


class PathAdjusterAgent:
    """Inserts review nodes and remediation tasks when mastery is insufficient."""

    name = "path_adjuster"
    mastery_threshold = 70

    def build_low_score_adjustment(self, plan: Any, node: Any, *, score: int, diagnosis: dict[str, Any] | None = None) -> Any | None:
        if score >= self.mastery_threshold:
            return None

        from backend.db.models import LearningPlanAdjustment, LearningPlanNode

        now = datetime.now()
        nodes = sorted(list(plan.nodes or []), key=lambda item: item.order_index)
        before_snapshot = {
            "node": self._node_snapshot(node),
            "plan": {
                "progress": int(plan.progress or 0),
                "mastery": int(plan.mastery or 0),
                "total_nodes": len(nodes),
            },
            "score": score,
            "diagnosis": diagnosis or {},
        }
        weak_points = [
            str(item) for item in (diagnosis or {}).get("weak_points", []) if str(item).strip()
        ][:3]
        review_task = f"复盘“{node.title}”薄弱点并补充 3 条解释"
        if weak_points:
            review_task = f"复盘“{node.title}”：{weak_points[0]}"

        existing_review = next(
            (
                item
                for item in nodes
                if item.parent_node_id == node.node_id
                and item.node_type == "review"
                and item.status in {"open", "learning", "review"}
            ),
            None,
        )

        created_node_id: str | None = None
        accepted = False
        node.status = "review"
        node.study_signals = {
            **(node.study_signals or {}),
            "needs_review": True,
            "latest_low_score": score,
            "weak_points": weak_points,
            "adjusted_by": self.name,
        }
        node.tasks = list(dict.fromkeys([*(node.tasks or []), review_task]))
        node.checks = list(dict.fromkeys([*(node.checks or []), node.check or f"能重新解释“{node.title}”"]))
        node.updated_at = now

        strategy = "mark_current_node_review"
        if existing_review:
            created_node_id = existing_review.node_id
            existing_review.status = "open"
            existing_review.updated_at = now
            accepted = True
            strategy = "reopen_review_node"
        elif len(nodes) < 10:
            insert_index = node.order_index + 1
            for item in nodes:
                if item.order_index >= insert_index:
                    item.order_index += 1
                    item.updated_at = now
            created_node_id = str(uuid4())
            plan.nodes.append(
                LearningPlanNode(
                    node_id=created_node_id,
                    parent_node_id=node.node_id,
                    order_index=insert_index,
                    title=f"复习：{node.title}",
                    subtitle="根据练习结果自动补强",
                    objective=f"修正“{node.title}”的薄弱点",
                    day=node.day,
                    node_type="review",
                    difficulty=node.difficulty,
                    estimated_minutes=max(20, min(node.estimated_minutes or 60, 45)),
                    status="open",
                    mastery=0,
                    resource=node.resource,
                    task=review_task,
                    check=node.check,
                    resources=node.resources or [],
                    tasks=[review_task, "重新完成节点练习"],
                    checks=node.checks or [node.check or f"能重新解释“{node.title}”"],
                    prerequisites=[node.node_id],
                    study_signals={
                        "adaptive": True,
                        "trigger": "low_quiz_score",
                        "source_node_id": node.node_id,
                        "score": score,
                        "weak_points": weak_points,
                        "adjusted_by": self.name,
                    },
                )
            )
            accepted = True
            strategy = "insert_review_node"
        else:
            accepted = True
            strategy = "mark_current_node_review_only"

        after_snapshot = {
            "node": self._node_snapshot(node),
            "created_node_id": created_node_id,
            "strategy": strategy,
            "agent": self.name,
        }
        reason = (
            "节点练习未达到 70 分，路径调整智能体自动加入复习闭环。"
            if strategy in {"insert_review_node", "reopen_review_node"}
            else "节点练习未达到 70 分；计划节点较多，当前仅标记需复习，未插入新节点。"
        )
        return LearningPlanAdjustment(
            plan_id=plan.plan_id,
            trigger_type="low_score",
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            reason=reason,
            accepted=accepted,
        )

    def _node_snapshot(self, node: Any) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "title": node.title,
            "status": node.status,
            "mastery": int(node.mastery or 0),
            "tasks": node.tasks or [],
            "checks": node.checks or [],
        }


class ExploreBridgeAgent:
    """Bridges custom-learning nodes to explore-style knowledge expansion."""

    name = "explore_bridge"

    def topic_for(self, *, goal: str, node_title: str) -> str:
        title = (node_title or "").strip()
        if title:
            return title
        return (goal or "").strip() or "自定义学习"

    def trace(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "decision": "explore_on_demand",
            "reason": "资料检索较重，放到节点学习过程中按需触发",
            "policy": "node_explore_endpoint",
            "confidence": 0.8,
            "impact": "节点学习时可从绑定知识库召回材料",
        }
