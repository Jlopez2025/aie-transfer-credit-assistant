"""Minimal Azure OpenAI REST client (stdlib only).
Reads creds from the file named by TRANSFER_ENV, else from .env next to this file."""
import os, json, time, urllib.request, urllib.error, ssl

_HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("TRANSFER_ENV") or os.path.join(_HERE, ".env")

def load_env():
    env = {}
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()
ENDPOINT = ENV["AZURE_OPENAI_ENDPOINT"].rstrip("/")
API_KEY = ENV["AZURE_OPENAI_API_KEY"]
API_VER = ENV["AZURE_OPENAI_API_VERSION"]
CHAT_DEP = ENV["AZURE_OPENAI_CHAT_DEPLOYMENT"]
EMBED_DEP = ENV["AZURE_OPENAI_EMBED_DEPLOYMENT"]

_CTX = ssl.create_default_context()

def _post(url, payload, timeout=120, retries=4):
    data = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("api-key", API_KEY)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            last = f"HTTP {e.code}: {body[:300]}"
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(last)
        except (urllib.error.URLError, TimeoutError) as e:
            last = f"URLError: {e}"
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(last)
    raise RuntimeError(last or "unknown error")

def embed(inputs):
    """inputs: list[str] -> list[list[float]] (order preserved)."""
    url = f"{ENDPOINT}/openai/deployments/{EMBED_DEP}/embeddings?api-version={API_VER}"
    j = _post(url, {"input": inputs})
    rows = sorted(j["data"], key=lambda d: d["index"])
    return [r["embedding"] for r in rows]

def chat(messages, temperature=0, max_tokens=400, json_mode=False):
    url = f"{ENDPOINT}/openai/deployments/{CHAT_DEP}/chat/completions?api-version={API_VER}"
    payload = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    j = _post(url, payload)
    return j["choices"][0]["message"].get("content") or ""

def chat_tools(messages, tools, deployment=None, temperature=0, max_tokens=1500):
    """One function-calling turn. Returns (assistant message dict, usage dict)."""
    dep = deployment or CHAT_DEP
    url = f"{ENDPOINT}/openai/deployments/{dep}/chat/completions?api-version={API_VER}"
    j = _post(url, {"messages": messages, "tools": tools, "tool_choice": "auto",
                    "temperature": temperature, "max_tokens": max_tokens})
    return j["choices"][0]["message"], j.get("usage", {})
