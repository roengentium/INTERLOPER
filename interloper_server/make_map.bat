@echo off
rem  Usage examples (run from anywhere):
rem    make_map.bat --game hl2 --seed 7 --compile
rem    make_map.bat --game portal --mode theme --ollama llama3 --hint "empty test chamber wing" --compile
rem    make_map.bat --game garrysmod --mode llm --llm-url https://YOURPROVIDER/v1 --llm-model MODEL --compile
rem  (for --llm-url set INTERLOPER_LLM_KEY first: set INTERLOPER_LLM_KEY=your-key)
cd /d "%~dp0"
py make_map.py %*
pause
