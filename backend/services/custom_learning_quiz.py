"""自定义学习测评域:出题(题型画像/质量门)与掌握度诊断评分。(拆自 services/custom_learning_agents.py)"""

from __future__ import annotations

import re

from typing import Any


class QuizAuthorAgent:
    """Authors node-level practice questions with multi-role coverage."""

    name = "quiz_author"

    def build(
        self,
        *,
        title: str,
        subtitle: str = "",
        objective: str = "",
        check: str = "",
        external_questions: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        profile = self._profile(title=title, subtitle=subtitle, objective=objective, check=check)
        candidates = [
            self._concept_question(profile),
            self._application_question(profile),
        ]
        candidates.extend(self._adapt_external_questions(external_questions or []))
        candidates.extend(
            [
                self._mechanism_question(profile),
                self._misconception_question(profile),
                self._transfer_question(profile),
            ]
        )
        max_count = 6 if external_questions else 5
        return self._quality_gate(candidates, max_count=max_count)

    def _profile(self, *, title: str, subtitle: str, objective: str, check: str) -> dict[str, Any]:
        joined = f"{title} {subtitle} {objective}"
        check_text = check or f"能说明“{title}”的关键概念、适用条件和常见误区。"
        profile = {
            "title": title,
            "check": check_text,
            "concept_question": f"学习“{title}”时，最需要先确认的是哪一项？",
            "concept_options": [
                {"key": "A", "text": "核心概念、适用条件和检查点"},
                {"key": "B", "text": "跳过基础，直接记忆结论"},
                {"key": "C", "text": "只看标题，不做练习"},
            ],
            "concept_answer": "A",
            "concept_analysis": "自定义学习节点必须围绕目标、条件和检查点完成闭环。",
            "mechanism_question": f"判断：只要记住“{title}”的定义，就等于掌握了本节点。",
            "mechanism_answer": "错误",
            "mechanism_analysis": "还需要能说明适用条件、关键步骤、判断依据和迁移案例。",
            "application_question": f"遇到一个和“{title}”相关的新案例时，第一步更应该做什么？",
            "application_options": [
                {"key": "A", "text": "识别输入、输出、约束条件和验收标准"},
                {"key": "B", "text": "直接套用结论，不检查条件"},
                {"key": "C", "text": "只复制资料中的一句定义"},
            ],
            "application_answer": "A",
            "application_analysis": "迁移应用前必须先确认问题边界和可验证标准。",
            "misconception_question": f"关于“{title}”，哪一种学习方式最容易造成后续节点卡住？",
            "misconception_options": [
                {"key": "A", "text": "只背术语，不解释条件、步骤和风险"},
                {"key": "B", "text": "用例子检验概念"},
                {"key": "C", "text": "对照验收标准复盘错因"},
            ],
            "misconception_answer": "A",
            "misconception_analysis": "只背术语无法支撑练习、诊断和后续迁移。",
            "transfer_question": f"请用一个具体案例说明“{title}”的关键判断依据。",
            "transfer_reference": check_text,
        }
        if "电荷灵敏" in joined:
            profile.update(
                {
                    "concept_question": "电荷灵敏前置放大器中，输入电荷 Q 与反馈电容 Cf 决定的理想输出阶跃近似为哪一项？",
                    "concept_options": [
                        {"key": "A", "text": "ΔV≈Q/Cf"},
                        {"key": "B", "text": "ΔV≈Q·Cf"},
                        {"key": "C", "text": "ΔV≈Rf/Cf"},
                    ],
                    "concept_analysis": "电荷被反馈电容积分，输出电压阶跃近似等于输入电荷除以反馈电容。",
                    "mechanism_question": "判断：电荷灵敏前放输入端近似虚地，有助于减小输入节点电压摆动并稳定电荷收集。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "虚地输入能降低探测器电容变化对读出增益的影响。",
                    "application_question": "某探测器输出电荷不变，若反馈电容 Cf 增大，理想输出阶跃会怎样变化？",
                    "application_options": [
                        {"key": "A", "text": "变小"},
                        {"key": "B", "text": "变大"},
                        {"key": "C", "text": "与 Cf 无关"},
                    ],
                    "application_analysis": "ΔV≈Q/Cf，Q 不变时 Cf 增大，输出阶跃变小。",
                    "misconception_question": "调试电荷灵敏前放输出过小时，哪种判断最可能是误区？",
                    "misconception_options": [
                        {"key": "A", "text": "只怀疑 ADC，不检查反馈电容和输入电荷估计"},
                        {"key": "B", "text": "核对 Q/Cf 估算"},
                        {"key": "C", "text": "检查输入端和反馈网络"},
                    ],
                    "misconception_analysis": "输出幅度首先应回到电荷积分关系、反馈参数和输入信号估计上排查。",
                    "transfer_question": "给定输入电荷和反馈电容时，你会如何估算电荷灵敏前放输出幅度，并判断结果是否合理？",
                    "transfer_reference": "先用 ΔV≈Q/Cf 估算输出阶跃，再结合输入动态范围、反馈复位、后级增益和噪声裕量判断。",
                }
            )
        elif "反馈" in joined or "复位" in joined:
            profile.update(
                {
                    "concept_question": "Rf-Cf 连续反馈中，输出尾脉冲的主要时间常数由什么决定？",
                    "concept_options": [
                        {"key": "A", "text": "τ=Rf·Cf"},
                        {"key": "B", "text": "τ=Rf/Cf"},
                        {"key": "C", "text": "τ=Cf/Rf"},
                    ],
                    "concept_analysis": "反馈电阻释放反馈电容电荷，形成由 Rf 和 Cf 乘积决定的指数衰减。",
                    "mechanism_question": "判断：反馈时间常数过长会增加高计数率下的脉冲堆积风险。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "尾脉冲变长会使基线恢复变慢，脉冲更容易重叠。",
                    "application_question": "如果输出长尾导致基线恢复慢，优先应检查哪组因素？",
                    "application_options": [
                        {"key": "A", "text": "Rf·Cf、成形时间和极零相消"},
                        {"key": "B", "text": "显示器亮度"},
                        {"key": "C", "text": "文件命名方式"},
                    ],
                    "application_analysis": "长尾和基线问题通常来自反馈时间常数、成形匹配和计数率条件。",
                    "transfer_reference": "应说明反馈释放路径、τ=Rf·Cf、基线恢复、堆积风险和复位方式取舍。",
                }
            )
        elif "噪声" in joined or "等效噪声" in joined or "ENC" in joined:
            profile.update(
                {
                    "concept_question": "输入电容增大时，前置放大器哪类噪声影响通常更容易变差？",
                    "concept_options": [
                        {"key": "A", "text": "串联噪声"},
                        {"key": "B", "text": "量化噪声"},
                        {"key": "C", "text": "宇宙线本底"},
                    ],
                    "concept_analysis": "串联噪声与输入级电压噪声和输入电容密切相关，输入电容越大通常 ENC 越差。",
                    "mechanism_question": "判断：ENC 是把输出噪声折算到输入端的等效电荷量。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "ENC 可直接用于比较前放和探测器组合对能量分辨率的影响。",
                    "application_question": "漏电流变大且成形时间较长时，哪类噪声更需要警惕？",
                    "application_options": [
                        {"key": "A", "text": "并联噪声"},
                        {"key": "B", "text": "采样混叠"},
                        {"key": "C", "text": "文件压缩噪声"},
                    ],
                    "application_analysis": "并联噪声与漏电流和反馈电阻热噪声密切相关，成形时间较长时更明显。",
                    "transfer_reference": "应关联输入电容、漏电流、成形时间、串联/并联噪声和 ENC 对分辨率的影响。",
                }
            )
        elif "接口" in joined or "ADC" in joined or "成形" in joined:
            profile.update(
                {
                    "concept_question": "前置放大器输出后接成形放大器的主要目的是什么？",
                    "concept_options": [
                        {"key": "A", "text": "改善信噪比、控制脉冲宽度并便于后级采集"},
                        {"key": "B", "text": "完全消除探测器物理涨落"},
                        {"key": "C", "text": "让反馈电容不再需要"},
                    ],
                    "concept_analysis": "成形用于滤波、改善信噪比、限制脉冲宽度，并匹配甄别或 ADC。",
                    "mechanism_question": "判断：ADC 量程选择需要同时考虑前放输出幅度、后级增益和动态范围。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "量程过小会饱和，过大又会浪费有效分辨率。",
                    "application_question": "若最大输入电荷增大，后级接口设计最需要重新核对哪一项？",
                    "application_options": [
                        {"key": "A", "text": "前放输出幅度、成形增益和 ADC 量程"},
                        {"key": "B", "text": "章节标题长度"},
                        {"key": "C", "text": "是否省略极零相消概念"},
                    ],
                    "application_analysis": "输入电荷变化会改变输出幅度和动态范围需求。",
                    "transfer_reference": "应说明最大输入电荷、反馈电容、前放输出、后级增益、ADC 量程和噪声裕量。",
                }
            )
        elif "参数" in joined or "诊断" in joined:
            profile.update(
                {
                    "concept_question": "如果前放输出经常饱和，优先排查哪类原因更合理？",
                    "concept_options": [
                        {"key": "A", "text": "反馈电容过小、输入电荷过大或复位异常"},
                        {"key": "B", "text": "教材页码太少"},
                        {"key": "C", "text": "ADC 位数一定过高"},
                    ],
                    "concept_analysis": "输出饱和通常来自增益过高、动态范围不足或反馈/复位链路异常。",
                    "mechanism_question": "判断：波形振荡和噪声过大只需要调低 ADC 采样率即可解决。",
                    "mechanism_answer": "错误",
                    "mechanism_analysis": "还需要检查输入电容、接地屏蔽、反馈布局和放大器稳定性。",
                    "application_question": "看到输出长尾和堆积时，你会优先检查哪些参数？",
                    "application_options": [
                        {"key": "A", "text": "反馈时间常数、成形时间、计数率和极零相消"},
                        {"key": "B", "text": "登录页配色"},
                        {"key": "C", "text": "报告页边距"},
                    ],
                    "application_analysis": "长尾和堆积与反馈恢复、成形匹配和事件间隔直接相关。",
                    "transfer_reference": "应能从输出过小、饱和、长尾、振荡或噪声过大反推可能原因。",
                }
            )
        return profile

    def _concept_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "choice",
            "question": profile["concept_question"],
            "options": profile["concept_options"],
            "answer": profile.get("concept_answer", "A"),
            "analysis": profile["concept_analysis"],
            "source_agent": "concept_examiner",
            "skill": "core_concept",
            "difficulty": "基础",
        }

    def _mechanism_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "judge",
            "question": profile["mechanism_question"],
            "options": [{"key": "正确", "text": "正确"}, {"key": "错误", "text": "错误"}],
            "answer": profile["mechanism_answer"],
            "analysis": profile["mechanism_analysis"],
            "source_agent": "mechanism_examiner",
            "skill": "principle_judgement",
            "difficulty": "理解",
        }

    def _application_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "choice",
            "question": profile["application_question"],
            "options": profile["application_options"],
            "answer": profile.get("application_answer", "A"),
            "analysis": profile["application_analysis"],
            "source_agent": "scenario_examiner",
            "skill": "application",
            "difficulty": "应用",
        }

    def _misconception_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "choice",
            "question": profile["misconception_question"],
            "options": profile["misconception_options"],
            "answer": profile.get("misconception_answer", "A"),
            "analysis": profile["misconception_analysis"],
            "source_agent": "misconception_diagnostician",
            "skill": "error_diagnosis",
            "difficulty": "诊断",
        }

    def _transfer_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "short",
            "question": profile["transfer_question"],
            "reference": profile["transfer_reference"],
            "source_agent": "transfer_coach",
            "skill": "transfer_explanation",
            "difficulty": "迁移",
        }

    def _adapt_external_questions(self, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for item in questions[:4]:
            if not isinstance(item, dict):
                continue
            stem = str(item.get("question") or item.get("stem") or "").strip()
            if not stem:
                continue
            qtype = str(item.get("type") or "short")
            question = {**item, "question": stem, "source_agent": "knowledge_base_question_agent"}
            question.setdefault("skill", "knowledge_recall")
            question.setdefault("difficulty", "资料")
            if qtype == "judge":
                question["options"] = [{"key": "正确", "text": "正确"}, {"key": "错误", "text": "错误"}]
                question.setdefault("answer", "正确")
            elif qtype == "choice" and not question.get("options"):
                continue
            elif qtype not in {"choice", "judge"}:
                question["type"] = "short"
                question.setdefault("reference", item.get("reference") or item.get("analysis") or "结合资料说明关键依据。")
            adapted.append(question)
        return adapted

    def _quality_gate(self, questions: list[dict[str, Any]], *, max_count: int) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in questions:
            stem = str(item.get("question") or "").strip()
            if not stem:
                continue
            key = stem.replace(" ", "")
            if key in seen:
                continue
            qtype = str(item.get("type") or "short")
            normalized = {**item, "question": stem, "type": qtype}
            if qtype in {"choice", "judge"}:
                options = normalized.get("options") or []
                if len(options) < 2 or not normalized.get("answer"):
                    continue
                normalized.setdefault("analysis", "对照节点详解和验收标准判断。")
            else:
                normalized["type"] = "short"
                normalized.setdefault("reference", "能结合概念、条件、步骤和案例说明判断依据。")
            accepted.append(normalized)
            seen.add(key)
            if len(accepted) >= max_count:
                break

        for index, item in enumerate(accepted, start=1):
            item["id"] = f"q{index}"
            item.setdefault("source_agent", self.name)
        return accepted


class MasteryDiagnosticianAgent:
    """Scores quiz attempts and surfaces weak points."""

    name = "mastery_diagnostician"

    _STOPWORDS = {
        "一个",
        "一种",
        "需要",
        "说明",
        "解释",
        "判断",
        "完成",
        "关键",
        "概念",
        "条件",
        "步骤",
        "案例",
        "依据",
        "节点",
        "学习",
        "资料",
        "标准",
        "能够",
        "可以",
        "以及",
        "进行",
        "相关",
    }

    def grade(self, questions: list[dict[str, Any]], answers: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not questions:
            return 0, {
                "weak_points": ["尚未生成练习"],
                "suggestions": ["请先生成节点练习"],
                "question_results": [],
            }
        total = len(questions)
        score_sum = 0
        weak_points: list[str] = []
        question_results: list[dict[str, Any]] = []

        for question in questions:
            qid = str(question.get("id", ""))
            answer = answers.get(qid)
            qtype = str(question.get("type") or "short")
            stem = str(question.get("question") or question.get("stem") or qid)
            result: dict[str, Any] = {
                "id": qid,
                "type": qtype,
                "question": stem,
                "user_answer": "" if answer is None else str(answer),
                "reference": str(question.get("reference") or ""),
                "analysis": str(question.get("analysis") or ""),
            }
            if qtype in {"choice", "judge"}:
                expected = str(question.get("answer", ""))
                correct = str(answer or "").strip().upper() == expected.upper()
                result.update(
                    {
                        "verdict": "correct" if correct else "wrong",
                        "score": 100 if correct else 0,
                        "expected_answer": expected,
                    }
                )
                if correct:
                    score_sum += 100
                else:
                    weak_points.append(stem)
            elif qtype == "multi":
                expected_set = set(str(question.get("answer", "")).upper())
                actual = set(str(answer or "").upper())
                if expected_set and expected_set == actual:
                    item_score = 100
                    verdict = "correct"
                elif expected_set and expected_set.intersection(actual):
                    item_score = 60
                    verdict = "partial"
                else:
                    item_score = 0
                    verdict = "wrong"
                result.update(
                    {
                        "verdict": verdict,
                        "score": item_score,
                        "expected_answer": str(question.get("answer", "")),
                    }
                )
                score_sum += item_score
                if verdict != "correct":
                    weak_points.append(stem)
            else:
                text = str(answer or "").strip()
                short_score, missing = self._score_short_answer(question, text)
                score_sum += short_score
                result.update(
                    {
                        "verdict": "correct" if not missing else ("partial" if short_score > 0 else "wrong"),
                        "score": short_score,
                        "missing_points": missing,
                    }
                )
                if missing:
                    weak_points.append(stem)
            question_results.append(result)

        score = round(score_sum / total)
        suggestions = ["回到节点详解，对照验收标准补充说明"] if weak_points else ["可以进入下一个节点"]
        return score, {
            "weak_points": weak_points,
            "suggestions": suggestions,
            "question_results": question_results,
        }

    def _score_short_answer(self, question: dict[str, Any], answer: str) -> tuple[int, list[str]]:
        if not answer:
            return 0, ["未作答"]

        reference_text = " ".join(
            str(question.get(key) or "")
            for key in ("reference", "reference_answer", "analysis", "answer")
        )
        scoring_points = question.get("scoring_points") or []
        if isinstance(scoring_points, list):
            reference_text = f"{reference_text} {' '.join(str(item) for item in scoring_points)}"

        expected_terms = self._keywords(reference_text)
        answer_terms = set(self._keywords(answer))
        if not expected_terms:
            completeness_markers = ("概念", "条件", "误区", "依据", "例子", "步骤", "关系", "应用", "风险", "结论")
            marker_count = sum(1 for marker in completeness_markers if marker in answer)
            if len(answer) >= 12 and marker_count >= 2:
                return 90, []
            if len(answer) >= 80:
                return 85, []
            if len(answer) >= 24:
                return 70, []
            return 45, ["答案过短"]

        covered = [term for term in expected_terms if term in answer_terms or term in answer]
        coverage = len(covered) / max(len(expected_terms), 1)
        length_bonus = 10 if len(answer) >= 80 else 5 if len(answer) >= 32 else 0
        score = round(min(95, coverage * 90 + length_bonus))
        if score >= 70:
            return score, []
        missing = [term for term in expected_terms if term not in covered][:5]
        return max(score, 30 if answer else 0), missing or ["参考要点覆盖不足"]

    def _keywords(self, text: str) -> list[str]:
        raw = re.findall(r"[A-Za-z][A-Za-z0-9_+\-/.]{1,}|[\u4e00-\u9fff]{2,}", text or "")
        terms: list[str] = []
        seen: set[str] = set()
        for item in raw:
            value = item.strip()
            if not value or value in self._STOPWORDS:
                continue
            if len(value) > 14 and re.fullmatch(r"[\u4e00-\u9fff]+", value):
                chunks = [value[i : i + 4] for i in range(0, len(value), 4)]
            else:
                chunks = [value]
            for chunk in chunks:
                if len(chunk) < 2 or chunk in self._STOPWORDS or chunk in seen:
                    continue
                seen.add(chunk)
                terms.append(chunk)
                if len(terms) >= 12:
                    return terms
        return terms
