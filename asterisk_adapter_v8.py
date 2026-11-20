#!/usr/bin/env python3
"""SIP test launcher: v8 dialogue with adapter2 playback."""
import asyncio
import inspect
import json
import os
import shlex
import urllib.request
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).resolve().with_name('.env'), override=False, interpolate=False)
    tts = os.environ.setdefault('AIVA_TTS', 'edge')
    provider = os.environ.setdefault('LLM_PROVIDER', 'local')
    if tts not in {'edge', 'piper'}:
        raise SystemExit('AIVA_TTS must be edge or piper.')
    if provider not in {'local', 'nvidia'}:
        raise SystemExit('LLM_PROVIDER must be local or nvidia.')
    if os.getenv('AIVA_LIVE_BOOKING', '0') != '0':
        raise SystemExit('This launcher is test-only: set AIVA_LIVE_BOOKING=0.')
    key = os.getenv('NVIDIA_API_KEY', '').strip()
    model = os.getenv('AIVA_NVIDIA_MODEL', '').strip()
    json_mode_flag = os.getenv('AIVA_NVIDIA_JSON_MODE', '1')
    if provider == 'nvidia' and (not key or not model or json_mode_flag not in {'0', '1'}):
        raise SystemExit('Set NVIDIA_API_KEY, AIVA_NVIDIA_MODEL and AIVA_NVIDIA_JSON_MODE=0 or 1.')

    import asterisk_adapter2 as sip
    import sim_zubar8 as agent

    log_text = os.getenv('AIVA_LOG_TEXT', '0') == '1'
    sip.agent = agent
    sip._EXTRACT_TAKES_STATE = len(inspect.signature(agent.extract).parameters) >= 3
    if os.getenv('AIVA_PIPER_EXTRA'):
        agent.PIPER_EXTRA = shlex.split(os.environ['AIVA_PIPER_EXTRA'])

    def nvidia_llm(messages, json_mode=False, max_tokens=160):
        payload = {'model': model, 'messages': messages, 'temperature': 0, 'max_tokens': max_tokens, 'stream': False}
        if json_mode and json_mode_flag == '1':
            payload['response_format'] = {'type': 'json_object'}
        request = urllib.request.Request('https://integrate.api.nvidia.com/v1/chat/completions', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(request, timeout=agent.HTTP_TIMEOUT) as response:
            choice = json.load(response)['choices'][0]
        text = choice['message'].get('content')
        if choice.get('finish_reason') == 'length' or not isinstance(text, str) or not text.strip():
            raise RuntimeError('NVIDIA response is empty or truncated.')
        return text.strip()

    def adapter_print(*args, **kwargs):
        if args and isinstance(args[0], str):
            line = args[0]
            if line.startswith(('[STATE] ', '[LLM] {')):
                return
            if line.startswith('[ASR] '):
                if log_text:
                    print('[ASR] KLIJENT:', line[6:], flush=True)
                return
        print(*args, **kwargs)

    async def speak(player, text, folder, disconnected):
        for index, part in enumerate(sip.split_sentences(text)):
            if disconnected.is_set():
                return
            pcm = await asyncio.to_thread(sip.tts_pcm, part, folder, 60 if index == 0 else 0, 90)
            if disconnected.is_set():
                return
            if not pcm:
                raise RuntimeError('TTS produced empty PCM.')
            player.enqueue(pcm)
            if log_text:
                print(f'[TTS] AGENT (u redu za slanje): {part}', flush=True)
        await player.wait_idle()
        await asyncio.sleep(0.15)

    if provider == 'nvidia':
        agent.llm = nvidia_llm
    sip.print = adapter_print
    sip.speak = speak
    print(f'[BOOT] Agent=v8 ASR=sr LLM={provider} TTS={sip.TTS_MODE} transcript={log_text}', flush=True)
    try:
        asyncio.run(sip.main())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
