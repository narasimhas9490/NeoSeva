from decimal import Decimal, InvalidOperation

from app.core.db import many, scalar
from app.core.errors import unprocessable
from app.shared.settings import text_for

CHOICE_TYPES = ("SINGLE_CHOICE", "MULTI_CHOICE")


def current_version(conn):
    """Return the template version currently served for every service.
    Versions are raised together, so one number covers all services.
    A catalog with no questions yet is version 1."""
    return scalar(conn, "SELECT COALESCE(MAX(template_version), 1) FROM request_question") or 1


def load_questions(conn, service_id):
    """Load the current questions for a service, each with its options.
    Ordered by sort_order, which is also the order showIf may point along.
    Returns (version, questions) where questions are raw rows."""
    version = current_version(conn)
    questions = many(
        conn,
        """SELECT * FROM request_question WHERE service_id = :s AND template_version = :v
           ORDER BY sort_order, id""",
        s=service_id,
        v=version,
    )
    options = many(
        conn,
        """SELECT o.* FROM request_question_option o
           WHERE o.question_id = ANY(:ids) ORDER BY o.sort_order, o.id""",
        ids=[q["id"] for q in questions] or [""],
    )
    for q in questions:
        q["options"] = [o for o in options if o["question_id"] == q["id"]]
    return version, questions


def _num(value):
    """Turn a stored NUMERIC into an int or float for JSON.
    0.5 stays 0.5 and 2.0 becomes 2.
    None stays None."""
    if value is None:
        return None
    d = Decimal(value)
    return int(d) if d == d.to_integral_value() else float(d)


def servable(question):
    """Say whether the app can draw a question at all.
    A choice question needs options; a number needs all four bounds.
    Anything else is left out rather than trapping somebody."""
    if question["type"] in CHOICE_TYPES:
        return bool(question["options"])
    return all(question[k] is not None for k in ("min_value", "max_value", "step_value", "default_value"))


def question_shape(question):
    """Put one question into the wire shape the apps draw from.
    Number questions carry unit, min, max, step and default.
    showIf appears only on dependent questions."""
    shape = {
        "id": question["id"],
        "type": question["type"],
        "label": question["label"],
        "required": question["required"],
        "allowNotSure": question["allow_not_sure"],
    }
    if question["type"] in CHOICE_TYPES:
        shape["options"] = [
            {"id": o["id"], "label": o["label"], **({"iconUrl": o["icon_url"]} if o["icon_url"] else {})}
            for o in question["options"]
        ]
    else:
        shape["unit"] = {
            "code": question["unit_code"],
            "label": question["unit_label"],
            "perLabel": question["unit_per_label"],
        }
        shape["min"] = _num(question["min_value"])
        shape["max"] = _num(question["max_value"])
        shape["step"] = _num(question["step_value"])
        shape["default"] = _num(question["default_value"])
    if question["depends_on_question_id"]:
        shape["showIf"] = {
            "questionId": question["depends_on_question_id"],
            "answerIn": list(question["depends_on_values"] or []),
        }
    return shape


def visible_ids(questions, answers):
    """Work out which questions are visible given the answers sent.
    A child is hidden when its parent is hidden, unanswered, not sure, or off-list.
    The server decides this itself and never trusts the app's choice."""
    visible = set()
    for q in questions:
        parent = q["depends_on_question_id"]
        if parent:
            answer = answers.get(parent)
            if parent not in visible or answer is None or answer["notSure"]:
                continue
            if not set(answer["values"]) & set(q["depends_on_values"] or []):
                continue
        visible.add(q["id"])
    return visible


def _parse_answers(raw_answers):
    """Normalise the answers array sent by the app into a dict.
    values is always a list of strings; notSure defaults to false.
    A duplicate or malformed entry is 422 INVALID_ANSWER_VALUE."""
    if raw_answers is None:
        raw_answers = []
    if not isinstance(raw_answers, list):
        raise unprocessable("INVALID_ANSWER_VALUE", "answers must be an array.")
    answers = {}
    for item in raw_answers:
        if not isinstance(item, dict) or not isinstance(item.get("questionId"), str):
            raise unprocessable("INVALID_ANSWER_VALUE", "Each answer needs a questionId.")
        qid = item["questionId"]
        values = item.get("values") or []
        if qid in answers or not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise unprocessable("INVALID_ANSWER_VALUE", "Answer values must be strings.", {"questionId": qid})
        answers[qid] = {"values": values, "unit": item.get("unit"), "notSure": bool(item.get("notSure", False))}
    return answers


def _check_number(question, answer):
    """Check a number answer against its unit and stepper bounds.
    Unparseable is INVALID_ANSWER_VALUE; outside min/max or off-step is ANSWER_OUT_OF_RANGE.
    Returns the parsed Decimal."""
    qid = question["id"]
    if len(answer["values"]) != 1:
        raise unprocessable("INVALID_ANSWER_VALUE", "A number answer carries one value.", {"questionId": qid})
    if answer["unit"] not in (None, question["unit_code"]):
        raise unprocessable("INVALID_ANSWER_VALUE", "The unit does not match the question.", {"questionId": qid})
    try:
        value = Decimal(answer["values"][0])
    except InvalidOperation:
        raise unprocessable("INVALID_ANSWER_VALUE", "The value is not a number.", {"questionId": qid})
    low, high, step = Decimal(question["min_value"]), Decimal(question["max_value"]), Decimal(question["step_value"])
    if not value.is_finite() or value < low or value > high or (step > 0 and (value - low) % step != 0):
        raise unprocessable(
            "ANSWER_OUT_OF_RANGE",
            "The number is outside the allowed range.",
            {"questionId": qid, "min": _num(low), "max": _num(high), "step": _num(step)},
        )
    return value


def validate_answers(conn, service_id, template_version, raw_answers, work_type_id):
    """Check a request's answers against the current template.
    Walks showIf itself, then checks required, notSure, options and ranges.
    Returns rows ready for request_answer, one per visible answered question."""
    version, questions = load_questions(conn, service_id)
    if template_version != version:
        raise unprocessable(
            "TEMPLATE_VERSION_STALE", "The questions have changed.", {"currentTemplateVersion": version}
        )
    questions = [q for q in questions if servable(q)]
    by_id = {q["id"]: q for q in questions}
    answers = _parse_answers(raw_answers)
    for qid in answers:
        if qid not in by_id:
            raise unprocessable("ANSWER_NOT_APPLICABLE", "That question is not asked.", {"questionId": qid})
    visible = visible_ids(questions, answers)
    rows = []
    for q in questions:
        answer = answers.get(q["id"])
        if q["id"] not in visible:
            if answer is not None:
                raise unprocessable("ANSWER_NOT_APPLICABLE", "That question is hidden.", {"questionId": q["id"]})
            continue
        if answer is None:
            if q["required"]:
                raise unprocessable("ANSWER_REQUIRED", "A required question has no answer.", {"questionId": q["id"]})
            continue
        if answer["notSure"]:
            if not q["allow_not_sure"]:
                raise unprocessable("NOT_SURE_NOT_ALLOWED", "Not sure is not offered here.", {"questionId": q["id"]})
            if answer["values"]:
                raise unprocessable("INVALID_ANSWER_VALUE", "Not sure carries no values.", {"questionId": q["id"]})
            rows.append({"question_id": q["id"], "values": [], "unit_code": q["unit_code"], "not_sure": True})
            continue
        if q["type"] == "NUMBER_WITH_UNIT":
            value = _check_number(q, answer)
            rows.append({"question_id": q["id"], "values": [str(_num(value))], "unit_code": q["unit_code"], "not_sure": False})
            continue
        option_ids = {o["id"] for o in q["options"]}
        values = answer["values"]
        count_ok = len(values) == 1 if q["type"] == "SINGLE_CHOICE" else len(values) >= 1
        if not count_ok or len(set(values)) != len(values) or not set(values) <= option_ids:
            raise unprocessable("INVALID_ANSWER_VALUE", "That option is not offered.", {"questionId": q["id"]})
        rows.append({"question_id": q["id"], "values": values, "unit_code": None, "not_sure": False})
    if work_type_id is not None:
        chosen = {v for r in rows if not r["not_sure"] for v in r["values"]}
        if work_type_id not in chosen:
            raise unprocessable("INVALID_ANSWER_VALUE", "workTypeId is not one of the answers.", {"field": "workTypeId"})
    return version, rows


def option_labels(conn, option_ids):
    """Look up option labels by id for display.
    Option ids are global primary keys, so no question is needed.
    Returns a dict from id to label."""
    if not option_ids:
        return {}
    rows = many(conn, "SELECT id, label FROM request_question_option WHERE id = ANY(:ids)", ids=list(option_ids))
    return {r["id"]: r["label"] for r in rows}


def formatted_answers(conn, request_id, all_texts):
    """Format a request's stored answers as heading plus sentence.
    Units use label for counts and perLabel for exactly one; not sure is words.
    The apps print displayValue as it is."""
    rows = many(
        conn,
        """SELECT a.question_id, a."values" AS vals, a.not_sure, q.type, q.label, q.short_label,
                  q.unit_label, q.unit_per_label, q.sort_order
           FROM request_answer a LEFT JOIN request_question q ON q.id = a.question_id
           WHERE a.request_id = :r ORDER BY q.sort_order NULLS LAST, a.question_id""",
        r=request_id,
    )
    labels = option_labels(conn, {v for r in rows for v in r["vals"]})
    out = []
    for r in rows:
        if r["not_sure"]:
            display = text_for(all_texts, "NOT_SURE", "Not sure")
        elif r["type"] == "NUMBER_WITH_UNIT" and r["vals"]:
            number = r["vals"][0]
            word = r["unit_per_label"] if number in ("1", "1.0") and r["unit_per_label"] else r["unit_label"]
            display = f"{number} {word}".strip() if word else number
        else:
            display = ", ".join(labels.get(v, v) for v in r["vals"])
        out.append(
            {"questionId": r["question_id"], "label": r["short_label"] or r["label"] or r["question_id"], "displayValue": display}
        )
    return out
