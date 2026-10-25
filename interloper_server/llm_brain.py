"""
llm_brain.py

Type 4 brain for any OpenAI-compatible chat API (OpenAI, voidai.app, OpenRouter, a local
llama.cpp/LM Studio server...). Same job as the Ollama brain: every few seconds pick ONE
high-level intent; the rule controller does the steering, so a slow, wrong or missing API
never breaks a demo.

    set INTERLOPER_LLM_KEY=sk-...                (never put the key on the command line)
    py server.py --llm-url https://<provider>/v1 --llm-model <model name>
"""

import json
import re
import urllib.request

from ollama_brain import OllamaBrain, SYSTEM_PROMPT


class OpenAIBrain(OllamaBrain):
    def __init__(self, base_url: str, model: str, api_key: str = "", interval: float = 3.0,
                 timeout: float = 20.0, ttl: float = 8.0):
        super().__init__(model=model, host=base_url, interval=interval, timeout=timeout, ttl=ttl)
        self.api_key = api_key

    def _url(self) -> str:
        u = self.host
        if u.endswith("/chat/completions"):
            return u
        return u + "/chat/completions"

    def _request(self, prompt: str) -> str:
        body = {
            "model": self.model,
            "temperature": 0.9,
            "max_tokens": 80,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        req = urllib.request.Request(self._url(), data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        text = data["choices"][0]["message"]["content"] or ""
        # some models wrap the JSON in prose or ```json fences
        m = re.search(r"\{.*\}", text, re.S)
        return m.group(0) if m else text
