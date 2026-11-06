"""
Small JSON stores under app/data/:
  domains.json             school -> official domain (found by search; editable)
  found_descriptions.json  descriptions found by "Find description", with source and search log
  decisions.json           evaluator approve/override decisions per transcript line
"""
import os, json, re, datetime, threading

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_LOCK = threading.Lock()

def _path(name):
    os.makedirs(DATA, exist_ok=True)
    return os.path.join(DATA, name)

def load(name):
    p = _path(name)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}

def save(name, obj):
    with _LOCK:
        tmp = _path(name) + ".tmp"
        json.dump(obj, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        os.replace(tmp, _path(name))

def version(name):
    p = _path(name)
    return os.path.getmtime(p) if os.path.exists(p) else 0

def now():
    return datetime.datetime.now().isoformat(timespec="seconds")

def course_key(inst, code, title):
    return "|".join([inst.strip().lower(), re.sub(r"\s+", " ", code.strip().upper()), title.strip().lower()])

def line_key(it):
    return "|".join([str(it["doc"]), it["code"], it["title"], it["term"]])
