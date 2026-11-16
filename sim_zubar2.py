#!/usr/bin/env python3
import json
import re
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"
PIPER_MODEL = str(Path.home() / "models/piper/sr_Marko_medium.onnx")

SYSTEM_PROMPT = """Ti si virtuelni recepcioner u stomatološkoj ordinaciji "Osmeh".
Odgovaraj isključivo na srpskom jeziku, kratko i ljubazno (najviše 2 rečenice).
Ne koristi Markdown, zvezdice, emotikone ni nabrajanja.

TRENUTNO SLOBODNI TERMINI:
- Sutra (Nedelja): 09:00, 10:30, 14:00
- Prekosutra (Ponedeljak): 11:00, 16:30

PRAVILA ZAKAZIVANJA:
1. Kada pacijent traži termin, ponudi mu slobodne termine sa liste iznad.
2. Kada izabere termin, pitaj ga za ime i prezime (ako već nije rekao).
3. Kada dobiješ termin i ime, ponovi podatke i reci da je termin uspešno zakazan i ne govori srecno, samo reci da je termin zakazan.
4. Ako korisnik traži neki termin koji nije na listi, ljubazno mu reci da taj termin nije slobodan i ponudi mu slobodne termine ponovo.
5. Ako korisnik kaze da moze da dodje sutra ili prekosutra, pitaj ga za tacan termin i vreme i ponudi mu slobodne termine samo za taj dan.
"""

# def record_audio(out_wav: Path, seconds: int = 5):
#     print(f"\n[SLUŠAM {seconds}s] Govori sada...")
#     subprocess.run(
#         [
#             "arecord", "-D", "default", "-q",
#             "-f", "S16_LE", "-r", "16000", "-c", "1",
#             "-d", str(seconds), str(out_wav),
#         ],
#         check=True,
#     )

def record_audio(out_wav: Path, seconds: int = 5):
    print(f"\n[SLUŠAM {seconds}s] Govori sada...")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "pulse", "-i", "default",
            "-t", str(seconds),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(out_wav),
        ],
        check=True,
    )


def transcribe(wav_path: Path) -> str:
    boundary = "mic-" + uuid.uuid4().hex
    body = bytearray()
    for name, val in [("response_format", "json"), ("language", "sr")]:
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode()
        )
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="in.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
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
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["choices"][0]["message"]["content"].strip()


def clean_for_tts(text: str) -> str:
    text = re.sub(r"[^\w\s.,!?čćžšđČĆŽŠĐ:-]", "", text)
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1 sati", text)
    text = re.sub(r"\b(\d{1,2}):(\d{2})\b", r"\1 i \2", text)
    return text.strip()


def speak(text: str):
    print(f"[ASISTENT]: {text}")
    tts_text = clean_for_tts(text)
    out_wav = "/tmp/asistent_odgovor.wav"
    subprocess.run(
        [
            "piper",
            "--model", PIPER_MODEL,
            "--length_scale", "1.05",
            "--noise_scale", "0.5",
            "--noise_w", "0.8",
            "--sentence_silence", "0.3",
            "--output_file", out_wav,
        ],
        input=tts_text.encode("utf-8"),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(["aplay", "-q", out_wav], check=True)


def main():
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    speak("Dobar dan, dobili ste stomatološku ordinaciju Gaavra. Kako mogu da vam pomognem?")

    with tempfile.TemporaryDirectory() as tmp:
        wav_file = Path(tmp) / "input.wav"
        while True:
            input("\nPritisni ENTER da postaviš pitanje (ili Ctrl+C za izlaz)...")
            record_audio(wav_file, seconds=5)

            t0 = time.time()
            stt_text = transcribe(wav_file)
            print(f"[TI ({time.time() - t0:.2f}s)]: {stt_text}")

            if not stt_text:
                speak("Nisam vas razumeo, možete li da ponovite?")
                continue

            if "doviđenja" in stt_text.lower() or "kraj" in stt_text.lower():
                speak("Hvala na pozivu, doviđenja.")
                break

            messages.append({"role": "user", "content": stt_text})
            t1 = time.time()
            answer = ask_llm(messages)
            print(f"[LLM vreme: {time.time() - t1:.2f}s]")

            messages.append({"role": "assistant", "content": answer})
            speak(answer)


if __name__ == "__main__":
    main()