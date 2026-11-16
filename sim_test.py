#!/usr/bin/env python3
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ti si lokalni glasovni asistent. Odgovaraj isključivo na srpskom jeziku, "
    "kratko i jasno u najviše dve rečenice. Ne koristi Markdown."
)

def make_test_wav(text: str, out_wav: Path):
    raw = out_wav.with_name("raw.wav")
    subprocess.run(["espeak-ng", "-v", "sr", text, "-w", str(raw)], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)],
        check=True,
    )

def transcribe(wav_path: Path) -> str:
    boundary = "sim-" + uuid.uuid4().hex
    body = bytearray()
    for name, val in [("response_format", "json"), ("language", "sr")]:
        body.extend(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
    body.extend(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"in.wav\"\r\nContent-Type: audio/wav\r\n\r\n".encode()
    )
    body.extend(wav_path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        WHISPER_URL,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp).get("text", "").strip()

def ask_llm(messages: list) -> str:
    payload = {"model": "local", "messages": messages, "temperature": 0.2, "max_tokens": 120}
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["choices"][0]["message"]["content"].strip()

def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    test_inputs = [
        "Dobar dan, da li radite subotom?",
        "Koliko košta šišanje?"
    ]

    for i, phrase in enumerate(test_inputs, 1):
        print(f"\n--- KRUG {i} ---")
        wav = Path(f"/tmp/sim_{i}.wav")
        make_test_wav(phrase, wav)
        print(f"1. [SIMULIRANI KORISNIK]: {phrase}")

        t0 = time.time()
        print("2. Šaljem na Whisper...")
        stt_text = transcribe(wav)
        print(f"3. [WHISPER ({time.time() - t0:.2f}s)]: {stt_text}")

        messages.append({"role": "user", "content": stt_text})
        t1 = time.time()
        print("4. Šaljem na LLM...")
        answer = ask_llm(messages)
        print(f"5. [LLM ({time.time() - t1:.2f}s)]: {answer}")

        messages.append({"role": "assistant", "content": answer})
        subprocess.run(["espeak-ng", "-v", "sr", answer], check=True)

if __name__ == "__main__":
    main()