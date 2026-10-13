"""Emotion-recognition survey: store answers and build the CSV report.

The survey page (web/survey.html, served at /survey) shows each expression on
the robot and asks the participant what they think it is. Each finished
session is POSTed here and appended to survey/responses.csv; the report files
are rebuilt from all responses every time:

    survey/responses.csv   one row per answer, every participant
    survey/report.csv      per emotion: recognition rate, confidence, time,
                           most common confusion (overall and per character)
    survey/confusion.csv   shown emotion x answered emotion, counts
"""

import csv
import os
import threading
import time
from collections import Counter, defaultdict

import expressions

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "survey")
RESPONSES = os.path.join(DIR, "responses.csv")
REPORT = os.path.join(DIR, "report.csv")
CONFUSION = os.path.join(DIR, "confusion.csv")

FIELDS = ["session", "participant", "timestamp", "character", "condition", "with_motion",
          "trial", "stimulus", "shown", "intensity", "valence", "arousal", "motion",
          "answer", "correct", "confidence", "response_time_s", "replays", "comment"]
_lock = threading.Lock()


def save(session):
    """Append one participant's session; rebuild the report. Returns row count."""
    rows = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    sid = session.get("session") or (time.strftime("%Y%m%d-%H%M%S") + "-"
                                     + str(session.get("participant", "anon")))
    for i, t in enumerate(session.get("trials", []), 1):
        shown = t.get("shown", "")
        answer = t.get("answer", "")
        rows.append({
            "session": sid, "participant": session.get("participant", ""),
            "timestamp": t.get("timestamp", stamp),
            "character": session.get("character", ""), "condition": session.get("condition", ""),
            "with_motion": int(bool(session.get("with_motion", True))),
            "trial": i, "stimulus": t.get("stimulus", ""), "shown": shown,
            "intensity": t.get("intensity", ""), "valence": t.get("valence", ""),
            "arousal": t.get("arousal", ""), "motion": t.get("motion", ""),
            "answer": answer, "correct": int(answer == shown),
            "confidence": t.get("confidence", ""),
            "response_time_s": round(float(t.get("response_time_s") or 0), 2),
            "replays": t.get("replays", 0), "comment": t.get("comment", ""),
        })
    with _lock:
        os.makedirs(DIR, exist_ok=True)
        new = not os.path.exists(RESPONSES)
        with open(RESPONSES, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
        report()
    return len(rows)


def _mean(xs):
    xs = [float(x) for x in xs if x not in ("", None)]
    return round(sum(xs) / len(xs), 2) if xs else ""


def report():
    """Rebuild report.csv and confusion.csv from every response so far."""
    if not os.path.exists(RESPONSES):
        return
    with open(RESPONSES, newline="") as fh:
        rows = list(csv.DictReader(fh))
    order = [k for k, _ in expressions.CHOICES]
    answers = order + ["unsure"]

    groups = defaultdict(list)
    for r in rows:
        for scope in ("all", r["character"]):
            groups[(scope, r["shown"], r["intensity"])].append(r)
            groups[(scope, "ALL", "")].append(r)

    def sort_key(k):
        scope, shown, inten = k
        return (scope != "all", scope, shown == "ALL",
                order.index(shown) if shown in order else 99, inten)

    with open(REPORT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["scope", "shown", "intensity", "n", "participants", "recognised_pct",
                    "mean_confidence", "mean_response_time_s", "most_confused_with",
                    "most_confused_pct", "answers"])
        for key in sorted(groups, key=sort_key):
            scope, shown, inten = key
            g = groups[key]
            n = len(g)
            hits = sum(r["correct"] == "1" for r in g)
            wrong = Counter(r["answer"] for r in g if r["correct"] != "1")
            top, top_n = (wrong.most_common(1)[0] if wrong else ("", 0))
            spread = Counter(r["answer"] for r in g)
            w.writerow([scope, shown, inten, n, len({r["participant"] for r in g}),
                        round(100 * hits / n, 1), _mean(r["confidence"] for r in g),
                        _mean(r["response_time_s"] for r in g), top,
                        round(100 * top_n / n, 1) if top else "",
                        "; ".join(f"{a} {c}" for a, c in spread.most_common())])

    with open(CONFUSION, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["shown \\ answered"] + answers + ["n", "recognised_pct"])
        for shown in order:
            g = [r for r in rows if r["shown"] == shown]
            if not g:
                continue
            c = Counter(r["answer"] for r in g)
            w.writerow([shown] + [c.get(a, 0) for a in answers]
                       + [len(g), round(100 * c.get(shown, 0) / len(g), 1)])
