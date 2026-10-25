Type 4 with an API brain (OpenAI-compatible: voidai.app, OpenAI, OpenRouter, LM Studio...)

1) In the server window's prompt, set your key for this session only (do not paste it in chat or
   put it on the command line):
       set INTERLOPER_LLM_KEY=your-key-here
2) Start the server (use the base URL and model name from the provider's docs):
       py server.py --port 27500 --llm-url https://<provider>/v1 --llm-model <model-name>
   Server log should say:  type 4 brain: API model <model> at <url>
3) In the game:  interlope 4
   Server log then shows  brain: <intent> (<why>)  lines.

Switch at runtime in the server window:  llm <url> <model>   |   llm off
Per demo from the game:  interlope 4 brain=ollama   (use Ollama)   or   brain=off   (rules only)
If the API is slow/unreachable the player plays on rules and logs a warning.
