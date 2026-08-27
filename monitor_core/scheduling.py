from __future__ import annotations


QUESTION_MODES = ("interleaved", "sequential")


def normalize_question_mode(value: object) -> str:
    mode = str(value or "interleaved").strip().lower()
    if mode not in QUESTION_MODES:
        raise ValueError("question_mode must be 'interleaved' or 'sequential'")
    return mode


def build_question_schedule(
    questions: list[str], rounds_per_question: int, mode: str
) -> list[str]:
    """Build a plan where every question is asked exactly N times."""
    rounds = int(rounds_per_question)
    if rounds < 1:
        raise ValueError("rounds_per_question must be greater than 0")
    normalized = normalize_question_mode(mode)
    if normalized == "sequential":
        return [question for question in questions for _ in range(rounds)]
    return [question for _ in range(rounds) for question in questions]
