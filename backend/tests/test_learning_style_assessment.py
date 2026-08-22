import pytest

from backend.services.learning_style_assessment import (
    DIMENSIONS,
    QUESTIONS,
    score_answers,
    validate_answers,
)


def test_questionnaire_has_four_dimensions_and_44_unique_questions() -> None:
    assert len(DIMENSIONS) == 4
    assert len(QUESTIONS) == 44
    assert len({question["id"] for question in QUESTIONS}) == 44
    assert all(
        len([question for question in QUESTIONS if question["dimension"] == dimension["id"]]) == 11
        for dimension in DIMENSIONS
    )


def test_all_a_answers_produce_strong_left_preferences() -> None:
    result = score_answers({question["id"]: "a" for question in QUESTIONS})

    assert result["scores"] == {
        "processing": 11,
        "perception": 11,
        "input": 11,
        "understanding": 11,
    }
    assert all(item["strength"] == "strong" for item in result["dimensions"])
    assert all(item["preferred_side"] == "left" for item in result["dimensions"])


def test_all_b_answers_produce_strong_right_preferences() -> None:
    result = score_answers({question["id"]: "b" for question in QUESTIONS})

    assert all(score == -11 for score in result["scores"].values())
    assert all(item["right_count"] == 11 for item in result["dimensions"])
    assert all(item["preferred_side"] == "right" for item in result["dimensions"])


def test_mixed_answers_calculate_counts_and_strength() -> None:
    answers = {}
    for dimension in DIMENSIONS:
        dimension_questions = [q for q in QUESTIONS if q["dimension"] == dimension["id"]]
        for index, question in enumerate(dimension_questions):
            answers[question["id"]] = "a" if index < 7 else "b"

    result = score_answers(answers)

    assert all(score == 3 for score in result["scores"].values())
    assert all(item["left_count"] == 7 for item in result["dimensions"])
    assert all(item["right_count"] == 4 for item in result["dimensions"])
    assert all(item["strength"] == "balanced" for item in result["dimensions"])


def test_validation_rejects_unknown_question_or_incomplete_submission() -> None:
    with pytest.raises(ValueError, match="未知题目"):
        validate_answers({"unknown": "a"})

    with pytest.raises(ValueError, match="全部 44 道题"):
        score_answers({QUESTIONS[0]["id"]: "a"})
