#!/usr/bin/env python3
import json
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"

SYSTEM_PROMPT = """Ti si virtuelni recepcioner u stomatološkoj ordinaciji "Osmeh".
Odgovaraj isključivo na srpskom jeziku, kratko i ljubazno (najviše 2 rečenice).
Ne koristi Markdown, zvezdice ni nabrajanja.

TRENUTNO SLOBODNI TERMINI:
- Sutra (ponedeljak): 09:00, 10:30, 14:00
- Prekosutra (utorak): 11:00, 16:30

PRAVILA ZAKAZIVANJA:
1. Kada pacijent traži termin, ponudi mu slobodne termine sa liste iznad.
2. Kada izabere termin, pitaj ga za ime i prezime (ako već nije rekao).
3. Kada dobiješ termin i ime, ponovi podatke i reci da je termin uspešno zakazan."""


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

    # Simulirani scenario: pacijent zakazuje zubara u 4 koraka
    scenario = [
        "Dobar dan, hteo bih da zakažem pregled kod zubara.",
        "Može li sutra ujutru, recimo oko pola jedanaest?",
        "Zovem se Marko Petrović.",
        "Hvala, doviđenja."
    ]

    for i, phrase in enumerate(scenario, 1):
        print(f"\n=== KORAK {i} ===")
        wav = Path(f"/tmp/zubar_{i}.wav")
        make_test_wav(phrase, wav)
        print(f"[PACIJENT (original)]: {phrase}")

        t0 = time.time()
        stt_text = transcribe(wav)
        print(f"[WHISPER ({time.time() - t0:.2f}s)]: {stt_text}")

        messages.append({"role": "user", "content": stt_text})
        t1 = time.time()
        answer = ask_llm(messages)
        print(f"[ASISTENT ({time.time() - t1:.2f}s)]: {answer}")

        messages.append({"role": "assistant", "content": answer})
        subprocess.run(["espeak-ng", "-v", "sr", answer], check=True)


if __name__ == "__main__":
    main()