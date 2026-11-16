#!/usr/bin/env python3
"""
zubar3.py - glasovni recepcioner (Whisper -> Qwen -> TTS).

TTS motori (--tts):
  piper         Piper sr_Marko_medium (staro ponašanje)
  omni-default  OmniVoice, srpski, automatski glas (bez reference)
  omni-custom   OmniVoice, srpski, glas klonira iz tvoje reference
  none          bez zvuka (samo ispis)

Ulaz (--input):
  mic           snimanje mikrofonom -> Whisper
  text          kucaš tekst (testira Qwen + logiku + TTS, bez mikrofona i Whispera)

Testovi:
  --selftest    logika zakazivanja, bez servera i bez zvuka (deterministički)
  --llm-test    5 rečenica kroz Qwen ekstrakciju (traži server na :8082)
  --tts-test    5 rečenica kroz izabrani TTS uz merenje vremena (--no-cache za pravo merenje)

Primeri:
  python zubar3.py --selftest
  python zubar3.py --tts omni-default --tts-test --no-cache
  python zubar3.py --tts omni-custom  --tts-test --no-cache
  python zubar3.py --tts omni-default --input text
  python zubar3.py --tts omni-custom  --input mic
"""
import argparse, hashlib, json, os, re, subprocess, sys, time, urllib.request, uuid
from pathlib import Path

WHISPER_URL = "http://127.0.0.1:8081/inference"
LLM_URL = "http://127.0.0.1:8082/v1/chat/completions"

BASE = Path.home() / "local-voice"
PIPER_MODEL = str(Path.home() / "models/piper/sr_Marko_medium.onnx")
PIPER_EXTRA = []
OMNI_MODEL_ID = "k2-fsa/OmniVoice"
REF_WAV = Path(os.environ.get("VOICE_REF_WAV", BASE / "voices/custom/reference.wav"))
REF_TXT = Path(os.environ.get("VOICE_REF_TXT", BASE / "voices/custom/reference.txt"))
CACHE_DIR = BASE / "cache" / "omnivoice"

CLINIC_NAME = "Mravinjak"
GREETING = f"Dobar dan, ordinacija {CLINIC_NAME}. Kako mogu da vam pomognem?"

WHISPER_PROMPT = (f"Stomatološka ordinacija {CLINIC_NAME}. Zakazivanje pregleda. "
                  "Sutra, prekosutra, danas. U devet, u deset i trideset, "
                  "u četrnaest, u jedanaest, u šesnaest i trideset. Ime i prezime.")

SLOTS = {"sutra": ["09:00", "10:30", "14:00"],
         "prekosutra": ["11:00", "16:30"]}
BOOKED = set()

CLINIC_FACTS = (f"Ordinacija {CLINIC_NAME}. Radno vreme: ponedeljak do petak od 8 do 20 časova "
                "(demo podatak, izmeni). Cene ne znaš.")

NUM = ["nula", "jedan", "dva", "tri", "četiri", "pet", "šest", "sedam", "osam",
       "devet", "deset", "jedanaest", "dvanaest", "trinaest", "četrnaest",
       "petnaest", "šesnaest", "sedamnaest", "osamnaest", "devetnaest",
       "dvadeset", "dvadeset jedan", "dvadeset dva", "dvadeset tri"]

TTS_TEST_SENTENCES = [
    GREETING,
    "Slobodno je sutra u devet časova ili u deset i trideset.",
    "Da potvrdim: Marko Petrović, sutra u četrnaest časova. Da li da zakažem?",
    "Taj termin nije slobodan. Slobodno je prekosutra u jedanaest časova.",
    "Hvala na pozivu, doviđenja.",
]


# ----------------------------------------------------------------- logika termina
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


# ----------------------------------------------------------------- Qwen
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
        f"Trenutna faza razgovora: {stage}. confirm je yes samo ako pacijent jasno potvrđuje "
        "i ne menja dan, vreme ni ime."
    )
    try:
        return json.loads(llm([{"role": "system", "content": sys_prompt},
                               {"role": "user", "content": text}], True, 100))
    except Exception as e:
        print(f"[GREŠKA ekstrakcije]: {e}")
        return {}


def answer_question(text):
    sys_prompt = ("Ti si recepcioner. Odgovori na srpskom u jednoj kratkoj rečenici, "
                  "bez emotikona i cifara. Koristi samo ove podatke, a ako nešto ne znaš, "
                  "reci da nemaš tu informaciju: " + CLINIC_FACTS)
    return llm([{"role": "system", "content": sys_prompt},
                {"role": "user", "content": text}])


def _val(x):
    return None if x in (None, "", "null", "None") else x


def next_reply(info, st, text, ask=answer_question):
    if info.get("intent") == "goodbye":
        return "Hvala na pozivu, doviđenja.", True
    prefix = ask(text) + " " if info.get("intent") == "question" else ""

    day, time_, name = _val(info.get("day")), _val(info.get("time")), _val(info.get("name"))
    has_change = (day in SLOTS) or bool(time_) or bool(name)

    if st["stage"] == "confirm" and info.get("confirm") == "yes" and not has_change:
        key = (st["day"], st["time"])
        if key in BOOKED:
            st.update(stage="slot", time=None)
            return f"Taj termin je u međuvremenu zauzet. Slobodno je {say_slots(st['day'])}.", False
        BOOKED.add(key)
        return (f"Zakazano je. {st['name']}, očekujemo vas {st['day']} u {say_time(st['time'])}. "
                "Hvala i doviđenja."), True
    if st["stage"] == "confirm" and info.get("confirm") == "no" and not has_change:
        st.update(stage="slot", time=None)
        return "U redu. Koji termin biste želeli umesto toga?", False
    if st["stage"] == "confirm" and has_change:
        st["stage"] = "slot"

    if day == "danas":
        return prefix + "Danas nema slobodnih termina. Slobodno je " + say_slots("sutra") + ".", False
    if day in SLOTS:
        if day != st["day"] and not time_:
            st["time"] = None
        st["day"] = day
    if time_:
        if not st["day"]:
            for d in SLOTS:
                if time_ in SLOTS[d]:
                    st["day"] = d
        if st["day"] and time_ in free(st["day"]):
            st["time"] = time_
        elif st["day"]:
            return prefix + f"Taj termin nije slobodan. Slobodno je {say_slots(st['day'])}.", False
    if name:
        st["name"] = name

    if not st["day"]:
        return prefix + f"Slobodni termini su {say_slots('sutra')}, i {say_slots('prekosutra')}. Koji vam odgovara?", False
    if not st["time"]:
        return prefix + f"Slobodno je {say_slots(st['day'])}. Koji termin želite?", False
    if not st["name"]:
        return prefix + f"Može, {st['day']} u {say_time(st['time'])}. Kako se zovete i prezivate?", False
    st["stage"] = "confirm"
    return f"Da potvrdim: {st['name']}, {st['day']} u {say_time(st['time'])}. Da li da zakažem?", False


def new_state():
    return {"stage": "slot", "day": None, "time": None, "name": None}


# ----------------------------------------------------------------- TTS motori
def clean_for_tts(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"[^\w\s.,!?;:\-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class TTS:
    """Zajednički interfejs: synth(text) -> (wav_putanja, sek_generisanja, sek_zvuka, iz_keša)."""
    name = "none"

    def synth(self, text):
        return None, 0.0, 0.0, False

    def speak(self, text, play=True):
        print(f"[ASISTENT]: {text}", flush=True)
        text = clean_for_tts(text)
        if not text or self.name == "none":
            return
        try:
            wav, gen, dur, cached = self.synth(text)
        except Exception as e:
            print(f"[TTS GREŠKA/{self.name}]: {type(e).__name__}: {e}", flush=True)
            return
        if cached:
            print(f"[TTS/{self.name}] iz keša ({dur:.2f}s zvuka)", flush=True)
        else:
            rtf = gen / dur if dur else float("nan")
            print(f"[TTS/{self.name}] generisanje {gen:.2f}s | zvuk {dur:.2f}s | RTF {rtf:.2f}", flush=True)
        if play and wav:
            subprocess.run(["aplay", "-q", str(wav)])


class PiperTTS(TTS):
    name = "piper"

    def synth(self, text):
        wav = "/tmp/asistent_piper.wav"
        t0 = time.perf_counter()
        r = subprocess.run(["piper", "--model", PIPER_MODEL, *PIPER_EXTRA, "--output_file", wav],
                           input=text.encode(), capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode()[-400:])
        gen = time.perf_counter() - t0
        import wave
        with wave.open(wav) as w:
            dur = w.getnframes() / w.getframerate()
        return wav, gen, dur, False


class OmniTTS(TTS):
    def __init__(self, custom, steps, device, use_cache):
        self.custom, self.steps, self.device, self.use_cache = custom, steps, device, use_cache
        self.name = "omni-custom" if custom else "omni-default"
        self.model = None
        self.prompt = None
        self.ref_hash = None
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if custom:
            if not REF_WAV.is_file() or not REF_TXT.is_file():
                raise FileNotFoundError(
                    f"Nedostaje referenca: {REF_WAV} i/ili {REF_TXT}. Pokreni ./record_reference.sh")
            self.ref_text = REF_TXT.read_text(encoding="utf-8").strip()
            if not self.ref_text:
                raise ValueError(f"{REF_TXT} je prazan")
            self.ref_hash = hashlib.sha256(REF_WAV.read_bytes() + self.ref_text.encode()).hexdigest()

    def load(self):
        if self.model is not None:
            return
        import torch
        from omnivoice import OmniVoice
        self._torch = torch
        t0 = time.perf_counter()
        print(f"[TTS] Učitavam {OMNI_MODEL_ID} ({self.device})...", flush=True)
        dtype = torch.float32 if self.device == "cpu" else torch.float16
        self.model = OmniVoice.from_pretrained(OMNI_MODEL_ID, device_map=self.device, dtype=dtype)
        if self.custom:
            print("[TTS] Pripremam custom glas iz reference (jednom)...", flush=True)
            with torch.inference_mode():
                self.prompt = self.model.create_voice_clone_prompt(
                    ref_audio=str(REF_WAV), ref_text=self.ref_text)
        print(f"[TTS] Spreman za {time.perf_counter()-t0:.1f}s | {self.name} | koraci={self.steps}", flush=True)

    def _key(self, text):
        sig = {"v": 3, "model": OMNI_MODEL_ID, "custom": self.custom, "steps": self.steps,
               "lang": "Serbian", "ref": self.ref_hash, "text": text}
        return hashlib.sha256(json.dumps(sig, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def synth(self, text):
        import soundfile as sf
        wav = CACHE_DIR / f"{self._key(text)}.wav"
        if self.use_cache and wav.exists():
            return wav, 0.0, sf.info(str(wav)).duration, True
        self.load()
        torch = self._torch
        kw = {"text": text, "language": "Serbian", "num_step": self.steps}
        if self.prompt is not None:
            kw["voice_clone_prompt"] = self.prompt
        t0 = time.perf_counter()
        with torch.inference_mode():
            audios = self.model.generate(**kw)
        gen = time.perf_counter() - t0
        audio = audios[0]
        if torch.is_tensor(audio):
            audio = audio.detach().cpu().numpy()
        if getattr(audio, "ndim", 1) > 1:
            audio = audio.squeeze()
        sr = self.model.sampling_rate
        tmp = wav.with_suffix(".tmp.wav")
        sf.write(str(tmp), audio, sr)
        tmp.replace(wav)
        return wav, gen, len(audio) / sr, False


def make_tts(args):
    if args.tts == "piper":
        return PiperTTS()
    if args.tts == "none":
        return TTS()
    return OmniTTS(args.tts == "omni-custom", args.steps, args.device, not args.no_cache)


# ----------------------------------------------------------------- audio ulaz
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


# ----------------------------------------------------------------- testovi
def run_selftest():
    """Deterministički test logike zakazivanja (bez Qwen-a, Whisper-a i zvuka)."""
    no_llm = lambda t: "Nemam tu informaciju."
    results = []

    def case(title, steps, expect):
        BOOKED.clear()
        st = new_state()
        reply, done = "", False
        for info in steps:
            reply, done = next_reply(info, st, "", no_llm)
        ok = expect(reply, done, st)
        results.append(ok)
        print(f"[{'OK ' if ok else 'GREŠKA'}] {title}\n        -> {reply}")

    case("Pun tok zakazivanja + potvrda",
         [{"intent": "book", "day": "sutra", "time": "09:00"},
          {"name": "Marko Petrović"},
          {"confirm": "yes"}],
         lambda r, d, s: d and "Zakazano" in r and ("sutra", "09:00") in BOOKED)

    case("Ime se ne zakazuje bez potvrde",
         [{"day": "sutra", "time": "09:00"}, {"name": "Ana Jović"}],
         lambda r, d, s: not d and "Da potvrdim" in r and not BOOKED)

    case("Termin van liste je odbijen",
         [{"day": "sutra", "time": "12:00"}],
         lambda r, d, s: "nije slobodan" in r and s["time"] is None)

    case("Danas nema termina",
         [{"day": "danas"}],
         lambda r, d, s: "Danas nema" in r)

    case("Promena dana u fazi potvrde ne potvrđuje stari termin",
         [{"day": "sutra", "time": "14:00"}, {"name": "Petar Petrović"},
          {"confirm": "yes", "day": "prekosutra", "time": "11:00"}],
         lambda r, d, s: not d and not BOOKED and s["day"] == "prekosutra"
                         and s["time"] == "11:00" and "Da potvrdim" in r)

    case("Promena samo dana briše staro vreme",
         [{"day": "sutra", "time": "14:00"}, {"day": "prekosutra"}],
         lambda r, d, s: s["time"] is None and "Koji termin" in r)

    case("Odbijanje potvrde vraća na izbor termina",
         [{"day": "sutra", "time": "10:30"}, {"name": "Ivana Ilić"}, {"confirm": "no"}],
         lambda r, d, s: not d and not BOOKED and s["time"] is None)

    case("Pozdrav završava razgovor",
         [{"intent": "goodbye"}],
         lambda r, d, s: d)

    BOOKED.clear()
    BOOKED.add(("sutra", "09:00"))
    st = new_state()
    r, d = next_reply({"day": "sutra", "time": "09:00"}, st, "", no_llm)
    ok = "nije slobodan" in r
    results.append(ok)
    print(f"[{'OK ' if ok else 'GREŠKA'}] Zauzet termin se ne nudi ponovo\n        -> {r}")
    BOOKED.clear()

    print(f"\nSelftest: {sum(results)}/{len(results)} prošlo.")
    return all(results)


LLM_TEST = [
    ("Hteo bih termin sutra u devet", {"day": "sutra", "time": "09:00"}),
    ("Može prekosutra u jedanaest", {"day": "prekosutra", "time": "11:00"}),
    ("Može li danas", {"day": "danas"}),
    ("Zovem se Marko Petrović", {"name": "Marko Petrović"}),
    ("Hvala, doviđenja", {"intent": "goodbye"}),
]


def run_llm_test():
    ok_all = True
    for text, expected in LLM_TEST:
        t0 = time.perf_counter()
        info = extract(text, "slot")
        dt = time.perf_counter() - t0
        bad = {k: (v, info.get(k)) for k, v in expected.items() if info.get(k) != v}
        ok_all &= not bad
        print(f"[{'OK ' if not bad else 'GREŠKA'}] ({dt:.2f}s) '{text}' -> {info}" + (f"  očekivano/dobijeno: {bad}" if bad else ""))
    print("LLM test:", "SVE OK" if ok_all else "ima odstupanja (pogledaj gore)")


def run_tts_test(tts, play):
    if tts.name == "none":
        print("Izaberi --tts piper | omni-default | omni-custom")
        return
    gens = []
    for i, s in enumerate(TTS_TEST_SENTENCES, 1):
        print(f"\n--- rečenica {i}/{len(TTS_TEST_SENTENCES)} ---")
        text = clean_for_tts(s)
        print(f"[ASISTENT]: {s}", flush=True)
        wav, gen, dur, cached = tts.synth(text)
        gens.append(gen)
        print(f"[TTS/{tts.name}] gen {gen:.2f}s | zvuk {dur:.2f}s | RTF {gen/dur if dur else float('nan'):.2f}"
              + (" | IZ KEŠA (nije merenje!)" if cached else ""), flush=True)
        if play and wav:
            subprocess.run(["aplay", "-q", str(wav)])
    print(f"\n{tts.name}: prosek {sum(gens)/len(gens):.2f}s | najduže {max(gens):.2f}s po rečenici")
    print("Cilj za telefon: prosek ispod ~1-2 s (moj predlog kriterijuma). Prvo učitavanje modela se ne računa u rečenice.")


# ----------------------------------------------------------------- razgovor
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", choices=["piper", "omni-default", "omni-custom", "none"], default="omni-default")
    ap.add_argument("--input", choices=["mic", "text"], default="mic")
    ap.add_argument("--steps", type=int, default=16, help="OmniVoice num_step (manje = brže, lošiji kvalitet)")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-play", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--llm-test", action="store_true")
    ap.add_argument("--tts-test", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if run_selftest() else 1)
    if args.llm_test:
        run_llm_test()
        return

    tts = make_tts(args)
    if args.tts_test:
        run_tts_test(tts, not args.no_play)
        return

    st = new_state()
    wav = Path("/tmp/ulaz.wav")
    play = not args.no_play
    print(f"[KONFIG] tts={tts.name} input={args.input} steps={args.steps}")
    tts.speak(GREETING, play)
    done = False
    while not done:
        t_in = time.perf_counter()
        if args.input == "mic":
            record(wav)
            t_in = time.perf_counter()
            text = transcribe(wav)
            print(f"[TI ({time.perf_counter()-t_in:.2f}s)]: {text}")
        else:
            text = input("\n[TI, kucaj] > ").strip()
        if not text:
            tts.speak("Nisam vas čuo. Možete li da ponovite?", play)
            continue
        t1 = time.perf_counter()
        info = extract(text, st["stage"])
        print(f"[PODACI ({time.perf_counter()-t1:.2f}s)]: {info}  [STANJE]: {st}")
        reply, done = next_reply(info, st, text)
        t2 = time.perf_counter()
        tts.speak(reply, play)
        print(f"[UKUPNO od izjave do kraja zvuka: {time.perf_counter()-t_in:.2f}s]")


if __name__ == "__main__":
    main()