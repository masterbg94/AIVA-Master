#!/usr/bin/env python3
import json
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"

SYSTEM_PROMPT = (
    "Ti si lokalni glasovni asistent. Odgovaraj isključivo na srpskom jeziku, "
    "kratko i jasno u najviše dve rečenice. Ne koristi Markdown, zvezdice ni nabrajanja."
)


def record_audio(out_wav: Path, seconds: int = 5):
    print(f"\n[SLUŠAM {seconds}s] Govori sada...")
    subprocess.run(
        [
            "arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
            "-d", str(seconds), str(out_wav)
        ],
        check=True,
    )


def transcribe(wav_path: Path) -> str:
    boundary = "mvp-" + uuid.uuid4().hex
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("text", "").strip()


def ask_llm(messages: list) -> str:
    payload = {
        "model": "local",
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 120,
    }
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp)["choices"][0]["message"]["content"].strip()


def speak(text: str):
    print(f"[ASISTENT]: {text}")
    subprocess.run(["espeak-ng", "-v", "sr", text], check=True)


def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    speak("Sistem je spreman. Recite nešto nakon što vidite poruku na ekranu.")

    with tempfile.TemporaryDirectory() as tmp:
        wav_file = Path(tmp) / "input.wav"
        while True:
            input("Pritisni ENTER za snimanje (ili Ctrl+C za izlaz)...")
            record_audio(wav_file, seconds=5)

            t0 = time.time()
            user_text = transcribe(wav_file)
            t_stt = time.time() - t0
            print(f"[TI ({t_stt:.2f}s)]: {user_text}")

            if not user_text:
                speak("Nisam vas razumeo, pokušajte ponovo.")
                continue

            if "doviđenja" in user_text.lower() or "kraj" in user_text.lower():
                speak("Doviđenja.")
                break

            messages.append({"role": "user", "content": user_text})
            t1 = time.time()
            answer = ask_llm(messages)
            t_llm = time.time() - t1
            print(f"[LLM vreme: {t_llm:.2f}s]")

            messages.append({"role": "assistant", "content": answer})
            speak(answer)


if __name__ == "__main__":
    main()