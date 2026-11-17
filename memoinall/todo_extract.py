"""Conservative, local extraction of actions, dependencies and date meanings.

No deadlines or preparation dates are invented. Original evidence is kept beside
the action title; ambiguous ownership and imported documents need review.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import textutil

VERSION = 2
DATE_RE = re.compile(
    r"(?<![\d])(?P<iso>20\d{2}[-./]\d{1,2}[-./]\d{1,2})(?!\d)"
    r"|(?P<ko>(?:20\d{2}년\s*)?\d{1,2}월\s*\d{1,2}일)"
    r"|(?<![\d/.-])(?P<short>\d{1,2}/\d{1,2})(?![\d/])"
    r"|(?<![가-힣])(?P<weekday>(?:(?:다다음\s*주|다음\s*주|이번\s*주|지난\s*주|차주|담주)\s*)?[월화수목금토일]요일)"
    r"|(?<![가-힣])(?P<relative>그저께|그제|어제|오늘|내일|모레|글피)(?=$|[^가-힣]|까지|부터|쯤|에)"
    r"|(?<![가-힣])(?P<week>다다음\s*주|다음\s*주|이번\s*주|지난\s*주|차주|담주)(?=$|[^가-힣]|까지|부터|쯤|중|에)"
)
NEGATIVE_RE = re.compile(r"(?:필요\s*없|안\s*해도|하지\s*않아도|취소(?:됨|함|했|$)|하지\s*말)")
WAIT_RE = re.compile(r"(?:회신|답변|승인|검토|견적|자료|연락).{0,12}(?:대기(?:\s*중)?|기다리는\s*중|기다림|기다리기로(?:\s*함)?)$|요청\s*(?:함|했음|해둠|했[다어]|드림)$")
CONDITION_RE = re.compile(r"^(.{1,60}?(?:오면|받으면|끝나면|완료되면|승인되면|도착하면|확정되면))\s*[,，]?\s*")
OWNER_RE = re.compile(
    r"^(?P<owner>@[\w가-힣]+?(?:님)?|[가-힣]{1,6}(?:님|대리|과장|팀장|부장|주임|담당자)|담당자)\s*(?:이|가|은|는)\s+"
)
NOUN_ACTION_RE = re.compile(r"(?:확인|검토|정리|준비|작성|제출|발송|보내|읽|받|비교|수정|공유|연락|예약|점검)(?:하)?기$")
DATED_ACTION_RE = re.compile(r"(?:확인|검토|정리|준비|작성|제출|발송|공유|연락|예약|점검|보고|전달|신청|수정)(?:\s*예정)?$")
CONTEXT_DATE_RE = re.compile(r"\s*(?:회의\s*(?:내용|기록|자료)|회의록|당시|에\s*(?:받은|했던|열린))")


def date_mentions(text: str, base: datetime) -> list[dict]:
    today = base.date()
    monday = today - timedelta(days=today.weekday())
    found = []
    for match in DATE_RE.finditer(text):
        value = match.group()
        compact = re.sub(r"\s+", "", value)
        start = end = None
        try:
            if match.lastgroup in {"iso", "ko", "short"}:
                parts = [int(n) for n in re.findall(r"\d+", value)]
                year, month, day = parts if len(parts) == 3 else [today.year, *parts]
                # A December note referring to January (and vice versa) crosses a year.
                if len(parts) == 2 and today.month == 12 and month == 1:
                    year += 1
                elif len(parts) == 2 and today.month == 1 and month == 12:
                    year -= 1
                start = end = date(year, month, day)
            elif match.lastgroup == "relative":
                delta = {"그저께": -2, "그제": -2, "어제": -1, "오늘": 0, "내일": 1, "모레": 2, "글피": 3}[value]
                start = end = today + timedelta(days=delta)
            else:
                offset = 14 if compact.startswith("다다음") else 7 if compact.startswith(("다음", "차주", "담주")) else -7 if compact.startswith("지난") else 0
                if match.lastgroup == "weekday":
                    weekday = "월화수목금토일".index(compact[-3])
                    start = monday + timedelta(days=offset + weekday)
                    if len(compact) == 3 and start < today:
                        start += timedelta(days=7)
                    end = start
                else:
                    start = monday + timedelta(days=offset)
                    end = start + timedelta(days=6)
        except ValueError:
            continue
        found.append({"start": start.isoformat(), "end": end.isoformat(), "label": value,
                      "span": match.span(), "range": start != end})
    return found


def timing(text: str, base: datetime) -> dict:
    result = {"due": None, "scheduled_for": None, "schedule_end": None, "date_label": ""}
    mentions = date_mentions(text, base)
    issue = "날짜 확인 필요" if len(list(DATE_RE.finditer(text))) != len(mentions) else ""
    deadlines, planned = [], []
    for mention in mentions:
        left, right = mention["span"]
        suffix = text[right:]
        prefix = text[max(0, left - 8):left]
        if re.match(r"\s*까지", suffix) or re.search(r"(?:마감|기한)\s*[:：]?\s*$", prefix):
            deadlines.append(mention)
        elif not CONTEXT_DATE_RE.match(suffix):
            # Past/context dates are evidence, not a date to do the work.
            if mention["end"] >= base.date().isoformat():
                planned.append(mention)
    if len(deadlines) == 1:
        item = deadlines[0]
        if item["range"]:
            # '다음 주까지' has week precision; don't invent a Sunday deadline.
            result.update(scheduled_for=item["start"], schedule_end=item["end"], date_label=item["label"] + "까지 (정확한 기한 미정)")
        else:
            result["due"] = item["start"]
    elif len(deadlines) > 1:
        issue = "기한이 여러 개여서 확인 필요"
    if len(planned) == 1:
        item = planned[0]
        result.update(scheduled_for=item["start"], schedule_end=item["end"], date_label=item["label"])
    if not mentions and re.search(r"(?:회의|행사|발표|출근|퇴근)\s*전(?:까지)?", text):
        issue = "관련 일정 확인 필요"
    if issue:
        result["date_label"] = issue
    return result


def action_title(text: str, base: datetime) -> str:
    from .extract import TAIL_NOISE_RE

    title = TAIL_NOISE_RE.sub("", text).strip()
    # Only remove dates that describe execution/deadline. Keep '11월 10일 회의'.
    for item in reversed(date_mentions(title, base)):
        start, end = item["span"]
        suffix = title[end:]
        if (start == 0 and item["end"] >= base.date().isoformat() and not CONTEXT_DATE_RE.match(suffix)) or re.match(r"\s*까지", suffix):
            tail = re.sub(r"^\s*(?:까지|에|쯤|중)?\s*", "", suffix, count=1)
            title = title[:start] + tail
    title = title.strip()
    endings = [
        (r"해봐야\s*(?:함|한다|겠다|해|됨)$", "해보기"),
        (r"봐야\s*(?:함|한다|겠다|해|됨)$", "보기"),
        (r"해야\s*(?:함|한다|겠다|해|됨)$", "하기"),
        (r"써야\s*(?:함|한다|겠다|해|됨)$", "쓰기"),
        (r"받아야\s*(?:함|한다|겠다|해|됨)$", "받기"),
        (r"읽어야\s*(?:함|한다|겠다|해|됨)$", "읽기"),
        (r"보내야\s*(?:함|한다|겠다|해|됨)$", "보내기"),
        (r"\s*(?:필요함|필요하다|필요있음|확인 바람)$", ""),
        (r"할\s*것$", "하기"), (r"볼\s*것$", "보기"),
        (r"읽을\s*것$", "읽기"), (r"만들\s*것$", "만들기"),
    ]
    for pattern, replacement in endings:
        if re.search(pattern, title):
            title = re.sub(pattern, replacement, title)
            break
    return re.sub(r"\s+", " ", title).strip(" ,:：") or text.strip()


def identity(text: str, base: datetime) -> str:
    title = action_title(text, base)
    title = DATE_RE.sub("", title)
    title = re.sub(r"(?:까지|하기로\s*(?:함|했다)|하기|필요함|해야\s*함)\s*$", "", title)
    return re.sub(r"[^\w가-힣]", "", title).casefold()


def extract_todos(text: str, base: datetime, source: str = "web") -> list[dict]:
    from .extract import TODO_MARK_RE, TODO_KEYWORD_RE, TAIL_NOISE_RE, _ends_with_action

    result = []
    seen = set()
    for line in text.splitlines():
        mark, keyword = TODO_MARK_RE.match(line), TODO_KEYWORD_RE.match(line)
        explicit = bool(mark or keyword)
        bodies = [mark.group(2) if mark else keyword.group(1)] if explicit else textutil.sentences(line)
        for index, body in enumerate(bodies):
            body = body.strip().strip("-*• ")
            core = TAIL_NOISE_RE.sub("", body).strip()
            if not core or core in seen or body.endswith(("?", "？")) or NEGATIVE_RE.search(core):
                continue
            waiting = bool(WAIT_RE.search(core))
            condition = CONDITION_RE.match(core)
            action = _ends_with_action(core) or bool(NOUN_ACTION_RE.search(core))
            if not action and date_mentions(core, base):
                action = bool(DATED_ACTION_RE.search(action_title(core, base)))
            if not (explicit or action or waiting):
                continue
            seen.add(core)
            evidence = body
            if waiting and index + 1 < len(bodies) and re.search(r"받기로|회신\s*예정|답변\s*예정", bodies[index + 1]):
                evidence += " " + bodies[index + 1]
            dates = timing(evidence, base)
            status, reason = "open", ""
            if waiting or condition:
                status = "waiting"
                reason = condition.group(1) + " 진행" if condition else "요청한 답변이나 자료를 기다리는 중"
                if waiting and dates["due"]:
                    # A promised reply is a follow-up day, not my delivery deadline.
                    dates.update(review_on=dates["due"], due=None, date_label="회신 예정일에 다시 확인")
                elif waiting and dates["scheduled_for"] and dates["scheduled_for"] == dates["schedule_end"]:
                    dates["review_on"] = dates["scheduled_for"]
            owner = OWNER_RE.match(action_title(core, base))
            if owner:
                status, reason = "review", f"{owner.group('owner')}의 일인지 확인해 주세요."
            if source in {"document", "redmine", "files", "collection", "shared"} and not explicit:
                status, reason = "review", "가져온 자료에서 찾았습니다. 내가 챙길 일인지 확인해 주세요."
            if "확인 필요" in dates["date_label"] and status == "open":
                status, reason = "review", dates["date_label"]
            if mark and mark.group(1).lower() == "x":
                status = "done"
            title_text = core[condition.end():] if condition else core
            title = action_title(title_text, base)
            if waiting:
                title = re.sub(r"요청\s*(?:함|했음|해둠|했[다어]|드림)$", "회신 확인", title)
            result.append({"text": evidence, "title": title[:500], "status": status,
                           "done": status == "done", "reason": reason,
                           "identity_key": identity(core, base), **dates})
    return result
