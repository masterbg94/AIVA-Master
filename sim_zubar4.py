#!/usr/bin/env python3

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import wave
from pathlib import Path


WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"

PIPER_MODEL = Path.home() / "models/piper/sr_Marko_medium.onnx"
PIPER_EXTRA = []

# Ostavi "default" ili upiši ime internog izvora iz:
# pactl list short sources
MIC_SOURCE = "default"

# None koristi podrazumevani PipeWire izlaz, kod tebe „iMac Speakers“.
# Po potrebi upiši stabilno ime node-a, npr. "imac_speakers".
PLAYBACK_TARGET = None

RECORD_SECONDS = 6
RECORD_TIMEOUT = RECORD_SECONDS + 8
PIPER_TIMEOUT = 30
PLAYBACK_TIMEOUT = 30
HTTP_TIMEOUT = 60

WHISPER_PROMPT = (
    "Stomatološka ordinacija Osmeh. Zakazivanje pregleda. "
    "Sutra, prekosutra, danas. U devet, u deset i trideset, "
    "u četrnaest, u jedanaest, u šesnaest i trideset. Ime i prezime."
)

SLOTS = {
    "sutra": ["09:00", "10:30", "14:00"],
    "prekosutra": ["11:00", "16:30"],
}
BOOKED = set()

CLINIC_FACTS = (
    "Ordinacija Osmeh. Radno vreme: ponedeljak do petak od 8 do 20 časova."
)

NUM = [
    "nula", "jedan", "dva", "tri", "četiri", "pet", "šest", "sedam",
    "osam", "devet", "deset", "jedanaest", "dvanaest", "trinaest",
    "četrnaest", "petnaest", "šesnaest", "sedamnaest", "osamnaest",
    "devetnaest", "dvadeset", "dvadeset jedan", "dvadeset dva",
    "dvadeset tri",
]


def log(stage, message):
    print(f"[{stage}] {message}", flush=True)


def check_requirements():
    missing = [
        command
        for command in ("ffmpeg", "piper", "pw-play")
        if shutil.which(command) is None
    ]
    if missing:
        raise RuntimeError(f"Nedostaju komande u PATH-u: {', '.join(missing)}")
    if not PIPER_MODEL.is_file():
        raise RuntimeError(f"Piper model nije pronađen: {PIPER_MODEL}")


def run_audio(stage, command, timeout, input_text=None):
    options = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "check": True,
    }

    if input_text is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = input_text

    try:
        subprocess.run(command, **options)
        return True
    except subprocess.TimeoutExpired:
        log(stage, f"{command[0]} se nije završio za {timeout} s.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        log(stage, f"{command[0]} exit={exc.returncode}: "
                   f"{detail[-1200:] or 'nema dodatne poruke'}")
    except OSError as exc:
        log(stage, f"Ne mogu da pokrenem {command[0]}: {exc}")

    return False


def wav_duration(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def record(out):
    input(f"\nENTER = počni snimanje od {RECORD_SECONDS} sekundi... ")
    out.unlink(missing_ok=True)

    log("MIC", f"Otvaram ulaz {MIC_SOURCE!r}. Govori sada.")

    ok = run_audio(
        "MIC",
        [
            "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "pulse", "-i", MIC_SOURCE,
            "-t", str(RECORD_SECONDS),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(out),
        ],
        timeout=RECORD_TIMEOUT,
    )

    if not ok:
        out.unlink(missing_ok=True)
        return False

    try:
        seconds = wav_duration(out)
    except (OSError, EOFError, wave.Error) as exc:
        log("MIC", f"WAV nije ispravan: {exc}")
        out.unlink(missing_ok=True)
        return False

    if seconds < RECORD_SECONDS - 1:
        log("MIC", f"Snimak je prekratak: {seconds:.1f} s.")
        out.unlink(missing_ok=True)
        return False

    log("MIC", f"Snimljeno {seconds:.1f} s: {out}")
    return True


def speak(text, out):
    log("ASISTENT", text)
    clean = re.sub(r"[^\w\s.,!?čćžšđČĆŽŠĐ-]", "", text).strip()
    if not clean:
        return False

    out.unlink(missing_ok=True)
    log("PIPER", "Generišem govor...")

    if not run_audio(
        "PIPER",
        ["piper", "--model", str(PIPER_MODEL), *PIPER_EXTRA,
         "--output_file", str(out)],
        timeout=PIPER_TIMEOUT,
        input_text=clean + "\n",
    ):
        return False

    if not out.is_file() or out.stat().st_size == 0:
        log("PIPER", "Nije napravljen izlazni WAV.")
        return False

    command = ["pw-play"]
    if PLAYBACK_TARGET:
        command += ["--target", PLAYBACK_TARGET]
    command.append(str(out))

    log("PLAYBACK", "Puštam govor kroz PipeWire...")
    if not run_audio("PLAYBACK", command, timeout=PLAYBACK_TIMEOUT):
        return False

    log("PLAYBACK", "pw-play je završio bez greške.")
    return True


def say_time(t):
    h, m = map(int, t.split(":"))
    if m == 0:
        return f"{NUM[h]} časova"
    return f"{NUM[h]} i {'trideset' if m == 30 else m}"


def free(day):
    return [t for t in SLOTS[day] if (day, t) not in BOOKED]


def say_slots(day):
    items = [say_time(t) for t in free(day)]
    if not items:
        return f"za {day} nema slobodnih termina"
    if len(items) == 1:
        return f"{day} u {items[0]}"
    return f"{day} u " + ", ".join(items[:-1]) + " ili u " + items[-1]


def http_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        return json.load(response)


def llm(messages, json_mode=False, max_tokens=120):
    payload = {
        "model": "local",
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    log("LLM", "Šaljem zahtev lokalnom modelu...")
    result = http_json(LLM_URL, payload)
    return result["choices"][0]["message"]["content"].strip()


def extract(text, stage):
    sys_prompt = (
        "Iz izjave pacijenta izvuci podatke. Vrati ISKLJUČIVO JSON oblika: "
        '{"intent":"book|question|goodbye|other","day":"sutra|prekosutra|danas|null",'
        '"time":"HH:MM ili null","name":"ime i prezime ili null","confirm":"yes|no|null"}. '
        "Vreme zapiši u 24h formatu (npr. 'u devet' = 09:00, "
        "'pola jedanaest' = 10:30, 'u dva popodne' = 14:00). "
        "Ime upiši u nominativu. "
        f"Trenutna faza razgovora: {stage}. "
        "confirm je yes samo ako pacijent jasno potvrđuje."
    )
    raw = llm(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": text},
        ],
        json_mode=True,
        max_tokens=100,
    )
    info = json.loads(raw)
    if not isinstance(info, dict):
        raise ValueError("LLM nije vratio JSON objekat.")
    return info


def answer_question(text):
    sys_prompt = (
        "Ti si recepcioner. Odgovori na srpskom u jednoj kratkoj rečenici, "
        "bez emotikona i cifara. Koristi samo ove podatke, a ako nešto ne znaš, "
        "reci da nemaš tu informaciju: " + CLINIC_FACTS
    )
    return llm([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text},
    ])


def next_reply(info, st, text):
    if info.get("intent") == "goodbye":
        return "Hvala na pozivu, doviđenja.", True

    prefix = ""
    if info.get("intent") == "question":
        prefix = answer_question(text) + " "

    day, time_, name = info.get("day"), info.get("time"), info.get("name")

    if st["stage"] == "confirm" and info.get("confirm") == "yes":
        key = (st["day"], st["time"])
        if key in BOOKED:
            st.update(stage="slot", time=None)
            return (
                f"Taj termin je u međuvremenu zauzet. "
                f"Slobodno je {say_slots(st['day'])}.",
                False,
            )

        BOOKED.add(key)
        return (
            f"Zakazano je. {st['name']}, očekujemo vas "
            f"{st['day']} u {say_time(st['time'])}. Hvala i doviđenja.",
            True,
        )

    if st["stage"] == "confirm" and info.get("confirm") == "no":
        st.update(stage="slot", time=None)
        return "U redu. Koji termin biste želeli umesto toga?", False

    if day == "danas":
        return (
            prefix + "Danas nema slobodnih termina. Slobodno je "
            + say_slots("sutra") + ".",
            False,
        )

    if day in SLOTS and day != st["day"]:
        # Termin iz prethodnog dana ne sme da ostane izabran.
        st.update(day=day, time=None, stage="slot")

    if time_ and time_ != "null":
        if not st["day"]:
            for candidate_day in SLOTS:
                if time_ in SLOTS[candidate_day]:
                    st["day"] = candidate_day
                    break

        if st["day"] and time_ in free(st["day"]):
            st["time"] = time_
        elif st["day"]:
            st.update(time=None, stage="slot")
            return (
                prefix + f"Taj termin nije slobodan. "
                f"Slobodno je {say_slots(st['day'])}.",
                False,
            )

    if name and name != "null":
        st["name"] = name

    if not st["day"]:
        return (
            prefix + f"Slobodni termini su {say_slots('sutra')} "
            f"i {say_slots('prekosutra')}. Koji vam odgovara?",
            False,
        )

    if not st["time"]:
        return (
            prefix + f"Slobodno je {say_slots(st['day'])}. Koji termin želite?",
            False,
        )

    if not st["name"]:
        return (
            prefix + f"Može, {st['day']} u {say_time(st['time'])}. "
            "Kako se prezivate i kako se zovete?",
            False,
        )

    st["stage"] = "confirm"
    return (
        f"Da potvrdim: {st['name']}, {st['day']} u {say_time(st['time'])}. "
        "Da li da zakažem?",
        False,
    )


def transcribe(path):
    boundary = "v2-" + uuid.uuid4().hex
    body = bytearray()

    for key, value in [
        ("response_format", "json"),
        ("language", "sr"),
        ("temperature", "0"),
        ("prompt", WHISPER_PROMPT),
    ]:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
            f"\r\n\r\n{value}\r\n"
        ).encode("utf-8")

    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        'filename="in.wav"\r\nContent-Type: audio/wav\r\n\r\n'
    ).encode("utf-8")
    body += path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        WHISPER_URL,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    log("WHISPER", "Šaljem WAV na transkripciju...")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        return json.load(response).get("text", "").strip()


def main():
    try:
        check_requirements()
    except RuntimeError as exc:
        log("START", str(exc))
        return 1

    st = {"stage": "slot", "day": None, "time": None, "name": None}

    # Svako pokretanje ima sopstvene fajlove i ne koristi stari snimak
    # iz prethodnog procesa. Fajlovi se brišu po izlasku.
    with tempfile.TemporaryDirectory(prefix="osmeh-audio-") as directory:
        folder = Path(directory)
        input_wav = folder / "ulaz.wav"
        output_wav = folder / "asistent.wav"

        speak("Dobar dan, ordinacija Osmeh. Kako mogu da vam pomognem?", output_wav)

        while True:
            if not record(input_wav):
                continue

            try:
                text = transcribe(input_wav)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                log("WHISPER", f"Zahtev nije uspeo: {exc}")
                continue

            log("TI", text or "(prazna transkripcija)")

            if not text:
                log("MIC", f"Za proveru snimka: pw-play {input_wav}")
                speak("Nisam vas čuo. Možete li da ponovite?", output_wav)
                continue

            try:
                info = extract(text, st["stage"])
                log("PODACI", f"{info} | STANJE: {st}")
                reply, done = next_reply(info, st, text)
            except (OSError, ValueError, KeyError, IndexError, TypeError,
                    AttributeError) as exc:
                log("LLM", f"Zahtev ili odgovor nije ispravan: {exc}")
                continue

            speak(reply, output_wav)
            if done:
                return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n[STOP] Izlaz.", flush=True)