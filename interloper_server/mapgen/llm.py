"""llm.py - tiny chat client for the map generator (Ollama or any OpenAI-compatible API)."""

import json
import os
import re
import urllib.request
from typing import Optional


class LLMClient:
    def __init__(self, kind: str, host: str, model: str, key: str = "", timeout: float = 120.0):
        self.kind = kind            # "ollama" | "openai"
        self.host = host.rstrip("/")
        self.model = model
        self.key = key
        self.timeout = timeout

    @classmethod
    def from_args(cls, ollama: Optional[str] = None, ollama_host: str = "http://127.0.0.1:11434",
                  llm_url: Optional[str] = None, llm_model: Optional[str] = None) -> Optional["LLMClient"]:
        if llm_url and llm_model:
            return cls("openai", llm_url, llm_model, os.environ.get("INTERLOPER_LLM_KEY", ""))
        if ollama:
            return cls("ollama", ollama_host, ollama)
        return None

    @classmethod
    def from_server(cls, srv) -> Optional["LLMClient"]:
        return cls.from_args(getattr(srv, "ollama_model", None), getattr(srv, "ollama_host", "http://127.0.0.1:11434"),
                             getattr(srv, "llm_url", None), getattr(srv, "llm_model", None))

    def ask(self, system: str, user: str, max_tokens: int = 4000, temperature: float = 0.8) -> str:
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if self.kind == "openai":
            url = self.host if self.host.endswith("/chat/completions") else self.host + "/chat/completions"
            body = {"model": self.model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
            headers = {"Content-Type": "application/json"}
            if self.key:
                headers["Authorization"] = "Bearer " + self.key
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return data["choices"][0]["message"]["content"] or ""
        body = {"model": self.model, "messages": msgs, "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens}}
        req = urllib.request.Request(self.host + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return data.get("message", {}).get("content", "")


def extract_json(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, dict) else None
    except ValueError:
        return None


def extract_vmf(text: str) -> str:
    """Pull VMF text out of a reply that may be wrapped in prose or ``` fences."""
    m = re.search(r"```(?:vmf|text)?\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text
    i = body.find("world")
    return body[i:] if i > 0 and "versioninfo" not in body[:i] else body
