# AGENTS.md — AIVA voice assistant (Asterisk + local LLM + Whisper)

## What this is
Serbian-language dental-clinic voice bot.
Linphone (ext. 601) → Asterisk PJSIP → AudioSocket (tcp 127.0.0.1:9092) → Python asyncio adapter → Whisper STT (:8081) → local LLM JSON intent (:8082) → deterministic `next_reply()` state machine → TTS (edge-tts cloud or Piper local) → 8 kHz PCM back to Asterisk.
The LLM only extracts JSON (`intent`, `date_iso`, `time`, `name`, `phone`, `confirm`); Python decides what to say.

## Main entrypoints
- `asterisk_adapter2.py` — AudioSocket server, port 9092. Imports `sim_zubar7 as agent` (hardcoded).
  Run: `source ~/local-voice/venv/bin/activate && AIVA_TTS=piper python3 -u asterisk_adapter2.py`
- `asterisk_adapter_v8.py` — V2 variant.
  Run: `LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0 python3 -u ./asterisk_adapter_v8.py`
- Docstring of `asterisk_adapter2.py` still says `asterisk_adapter.py`; the real file is `asterisk_adapter2.py`.

## Start order (from zero)
1. llama.cpp server (port 8082):
   `cd ~/llama.cpp && ./build/bin/llama-server -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M --device Vulkan0 -ngl 99 --host 127.0.0.1 --port 8082 -c 4096` (one line; wrapping without `\` breaks the shell)
2. whisper.cpp (port 8081): `cd ~/whisper.cpp/build/bin && ./whisper-server -m ../../models/ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8081 -l sr -t 4`
3. Asterisk: `sudo systemctl enable --now asterisk`, then `sudo asterisk -rvvv`
4. Adapter last (see entrypoints above)
5. Call `601` from Linphone (registered on Asterisk).

Ports: llama 8082, whisper 8081, adapter 9092, SIP 5060. Two adapters cannot run at once (port 9092 → Address already in use).

## Required tools / files
- `ffmpeg` in PATH (soxr preferred; falls back to aresample). Adapter exits if missing.
- `piper` in PATH only when `AIVA_TTS=piper`.
- Piper model: `~/models/piper/sr_Marko_medium.onnx` (+ `.onnx.json`).
- Venv: `~/local-voice/venv` (`source ~/local-voice/venv/bin/activate`). `pip install edge-tts piper-tts` (google-api-python-client google-auth only for Google Calendar).
- No `requirements.txt`, `setup.py`, `pyproject.toml`, pytest, or CI in repo. `requirements-v8.txt` lists only `python-dotenv` + `edge-tts`.

## Health checks
- `curl -s http://127.0.0.1:8082/health` (llama)
- `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/` (whisper; expect 200)
- `ss -ltnp | grep 9092` (adapter listening)
- Asterisk CLI: `sudo asterisk -rx "pjsip show endpoints"`, `pjsip show contacts`, `dialplan show`, `module show like audiosocket`

## Environment variables
`.env` is gitignored; copy from `.env.example`. Shell env wins over `.env`.
- `AIVA_TTS=edge|piper` (default edge → Microsoft cloud, needs internet; piper → fully local)
- `AIVA_LIVE_BOOKING=0|1` (0 = test mode, no real reservations)
- `AIVA_RMS_THRESHOLD=250`, `AIVA_END_SILENCE_MS=900`, `AIVA_MAX_TURNS=20` (VAD)
- `AIVA_TARGET_RMS=2600`, `AIVA_PEAK=26000`, `AIVA_PIPER_CYR`, `AIVA_DUMP_DIR`, `AIVA_PIPER_EXTRA`
- Agent: `WHISPER_URL`, `LLM_URL`, `LLM_PROVIDER=local|nvidia`, `GOOGLE_SA_FILE`, `GOOGLE_CALENDAR_ID`, `OWNER_PHONE_NUMBER`

## Logs / debugging
- Run adapter in foreground with `python3 -u` for line-buffered logs: `[SIP]`, `[VAD]`, `[ASR] '...'`, `[LLM] {...}`, `[STATE] {...}`.
- If you only see "Slušam na 127.0.0.1:9092" with no call activity: the call isn't reaching the adapter → check Asterisk AudioSocket module, dialplan, or adapter busy.
- `AIVA_DUMP_DIR=/tmp/aiva` saves outbound PCM; play with `ffplay -f s16le -ar 8000 -ac 1 /tmp/aiva/<file>.raw`.
- `[ASR]`/`[STATE]` print name+phone in plain text; `mask_pii()` exists in the agent but the adapter doesn't use it.

## Syntax check
`python3 -m py_compile asterisk_adapter2.py sim_zubar7.py sim_zubar8_fixed.py`
`sim_zubar7.py` and `sim_zubar8.py` are byte-identical duplicates.

## Asterisk config (NOT in this repo)
`/etc/asterisk/extensions.conf` and `pjsip.conf` are system files outside the repo. Dialplan for 601:
```
exten => 601,1,Answer()
 same => n,Set(CALLUUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,AudioSocket(${CALLUUID},127.0.0.1:9092)
 same => n,Hangup()
```
AudioSocket needs UUID in 8-4-4-4-12 form; `${UNIQUEID}` is invalid. Do NOT edit Asterisk config just because you changed the Python script — only if port/host changes, AudioSocket isn't used, or you add test numbers (e.g. 899 echo, 600 health). Reload after edits: `dialplan reload` / `module reload res_pjsip.so`.

## sim_zubar8_fixed.py
Uncertain whether standalone (own AudioSocket server on 9092) or agent-only module. Verify BEFORE running: `grep -n "9092\|start_server\|__main__" sim_zubar8_fixed.py`. If standalone, run with `LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0 python3 -u ./sim_zubar8_fixed.py` and do NOT run it alongside `asterisk_adapter2.py` (port conflict). If agent-only, it must be wired into the adapter (adapter hardcodes `sim_zubar7`).

## Gotchas
- `osmeh_bookings.db` and `__pycache__/` are tracked in git but contain potentially patient data / build artifacts; `.gitignore` does not exclude them.
- `OWNER_PHONE_NUMBER` is hardcoded in `sim_zubar7/8` (real number) — treat as PII.
- `.env` in the repo root currently holds an NVIDIA API key — never commit it; `.gitignore` excludes `.env` and `.env.*` except `!.env.example`.
