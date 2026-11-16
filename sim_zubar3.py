#!/usr/bin/env python3
import json, re, subprocess, uuid, urllib.request
from pathlib import Path

WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"
PIPER_MODEL = str(Path.home() / "models/piper/sr_Marko_medium.onnx")
PIPER_EXTRA = []  # npr. ["--length_scale", "1.05"]; proveri piper --help

WHISPER_PROMPT = ("Stomatološka ordinacija Osmeh. Zakazivanje pregleda. "
                  "Sutra, prekosutra, danas. U devet, u deset i trideset, "
                  "u četrnaest, u jedanaest, u šesnaest i trideset. Ime i prezime.")

SLOTS = {"sutra": ["09:00", "10:30", "14:00"],
         "prekosutra": ["11:00", "16:30"]}
BOOKED = set()

CLINIC_FACTS = ("Ordinacija Osmeh. Radno vreme: ponedeljak do petak od 8 do 20 časova.")

NUM = ["nula", "jedan", "dva", "tri", "četiri", "pet", "šest", "sedam", "osam",
       "devet", "deset", "jedanaest", "dvanaest", "trinaest", "četrnaest",
       "petnaest", "šesnaest", "sedamnaest", "osamnaest", "devetnaest",
       "dvadeset", "dvadeset jedan", "dvadeset dva", "dvadeset tri"]


def say_time(t):
    h, m = map(int, t.split(":"))
    return f"{NUM[h]} časova" if m == 0 else f"{NUM[h]} i {'trideset' if m == 30 else m}"


def free(day):
    return [t for t in SLOTS[day] if (day, t) not in BOOKED]


def say_slots(day):
    items = [say_time(t) for t in free(day)]
    if not items:
        return f"za {day} nema slobodnih termina"
    return f"{day} u " + ", ".join(items[:-1]) + (" ili u " if len(items) > 1 else "") + items[-1]


def http_json(url, payload, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def llm(messages, json_mode=False, max_tokens=120):
    p = {"model": "local", "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    if json_mode:
        p["response_format"] = {"type": "json_object"}
    return http_json(LLM_URL, p)["choices"][0]["message"]["content"].strip()


def extract(text, stage):
    sys_prompt = (
        "Iz izjave pacijenta izvuci podatke. Vrati ISKLJUČIVO JSON oblika: "
        '{"intent":"book|question|goodbye|other","day":"sutra|prekosutra|danas|null",'
        '"time":"HH:MM ili null","name":"ime i prezime ili null","confirm":"yes|no|null"}. '
        "Vreme zapiši u 24h formatu (npr. 'u devet' = 09:00, 'pola jedanaest' = 10:30, "
        "'u dva popodne' = 14:00). Ime upiši u nominativu. "
        f"Trenutna faza razgovora: {stage}. confirm je yes samo ako pacijent jasno potvrđuje."
    )
    try:
        return json.loads(llm([{"role": "system", "content": sys_prompt},
                               {"role": "user", "content": text}], True, 100))
    except Exception:
        return {}


def answer_question(text):
    sys_prompt = ("Ti si recepcioner. Odgovori na srpskom u jednoj kratkoj rečenici, "
                  "bez emotikona i cifara. Koristi samo ove podatke, a ako nešto ne znaš, "
                  "reci da nemaš tu informaciju: " + CLINIC_FACTS)
    return llm([{"role": "system", "content": sys_prompt},
                {"role": "user", "content": text}])


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
            return f"Taj termin je u međuvremenu zauzet. Slobodno je {say_slots(st['day'])}.", False
        BOOKED.add(key)
        return (f"Zakazano je. {st['name']}, očekujemo vas {st['day']} u {say_time(st['time'])}. "
                "Hvala i doviđenja."), True
    if st["stage"] == "confirm" and info.get("confirm") == "no":
        st.update(stage="slot", time=None)
        return "U redu. Koji termin biste želeli umesto toga?", False

    if day == "danas":
        return prefix + "Danas nema slobodnih termina. Slobodno je " + say_slots("sutra") + ".", False
    if day in SLOTS:
        st["day"] = day
    if time_ and time_ != "null":
        if not st["day"]:
            for d in SLOTS:
                if time_ in SLOTS[d]:
                    st["day"] = d
        if st["day"] and time_ in free(st["day"]):
            st["time"] = time_
        elif st["day"]:
            return prefix + f"Taj termin nije slobodan. Slobodno je {say_slots(st['day'])}.", False
    if name and name != "null":
        st["name"] = name

    if not st["day"]:
        return prefix + f"Slobodni termini su {say_slots('sutra')}, i {say_slots('prekosutra')}. Koji vam odgovara?", False
    if not st["time"]:
        return prefix + f"Slobodno je {say_slots(st['day'])}. Koji termin želite?", False
    if not st["name"]:
        return prefix + f"Može, {st['day']} u {say_time(st['time'])}. Kako se prezivate i kako se zovete?", False
    st["stage"] = "confirm"
    return (f"Da potvrdim: {st['name']}, {st['day']} u {say_time(st['time'])}. "
            "Da li da zakažem?"), False


def record(out):
    input("\nENTER = počni snimanje... ")
    p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "pulse", "-i", "default",
                          "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)],
                         stdin=subprocess.PIPE)
    input("[SNIMAM] Govori, pa ENTER = kraj... ")
    p.communicate(b"q")


def transcribe(path):
    b = "v2-" + uuid.uuid4().hex
    body = bytearray()
    for k, v in [("response_format", "json"), ("language", "sr"),
                 ("temperature", "0"), ("prompt", WHISPER_PROMPT)]:
        body += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    body += (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="in.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode()
    body += path.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(WHISPER_URL, data=bytes(body),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r).get("text", "").strip()


def speak(text):
    print(f"[ASISTENT]: {text}")
    clean = re.sub(r"[^\w\s.,!?čćžšđČĆŽŠĐ-]", "", text)
    wav = "/tmp/asistent.wav"
    r = subprocess.run(["piper", "--model", PIPER_MODEL, *PIPER_EXTRA, "--output_file", wav],
                       input=clean.encode(), capture_output=True)
    if r.returncode != 0:
        print("PIPER GREŠKA:", r.stderr.decode()[-400:])
        return
    subprocess.run(["aplay", "-q", wav])


def main():
    st = {"stage": "slot", "day": None, "time": None, "name": None}
    wav = Path("/tmp/ulaz.wav")
    speak("Doabr dan, ordinacija Osmeh. Kako mogu da vam pomognem?")
    done = False
    while not done:
        record(wav)
        text = transcribe(wav)
        print(f"[TI]: {text}")
        if not text:
            speak("Nisam vas čuo. Možete li da ponovite?")
            continue
        info = extract(text, st["stage"])
        print(f"[PODACI]: {info}  [STANJE]: {st}")
        reply, done = next_reply(info, st, text)
        speak(reply)


if __name__ == "__main__":
    main()