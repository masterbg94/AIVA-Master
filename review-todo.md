## 0. Šta je pregledano, a šta NIJE (iskreno)

Pročitano u celosti: struktura repoa, `readme.md`, `readme2.md`, `todo.md`, `asterisk_adapter2.py`.

Pročitano delimično: `sim_zubar7.py`, `sim_zubar8_fixed.py`. GitHub konektor je odsekao sadržaj posle `extract()` prompta (otprilike prvih 2/3 `sim_zubar7`, još manje `fixed`). Nisam video: `next_reply()`, dijaloški state machine, glavnu petlju i sve što je u `fixed` iznad 41 KB.

Nije čitano: `sim_test.py`, `sim_zubar.py`–`sim_zubar5.py`, `my_voice_test.py`, `omniVoiceTest/`, `osmeh_bookings.db` (binarno), `.vscode/`.

Posebno: Asterisk konfiguracija (`/etc/asterisk/*.conf`) NIJE u repou. Zato su dole primeri šabloni, a ne tvoj stvarni config. Svaka tvrdnja je označena kao [VIDLJIVO U KODU], [ZAKLJUČAK] ili [PROVERI KOMANDOM].

## 1. Arhitektura – kako sve radi

```
Linphone (601)
   │ SIP/RTP (ulaw/alaw, 8 kHz)
   ▼
Asterisk (PJSIP + dialplan)
   │ AudioSocket(uuid,127.0.0.1:9092)  ← TCP, slin 8 kHz, 16-bit, mono
   ▼
asterisk_adapter2.py  (Python asyncio server)
   ├─ VAD (RMS prag) → ceo iskaz pacijenta
   ├─ ffmpeg → 16 kHz WAV → Whisper server  :8081 (/inference)  = STT
   ├─ sim_zubar7.extract() → llama-server   :8082 (/v1/chat/completions) = JSON intent
   ├─ sim_zubar7.next_reply() = deterministički state machine zakazivanja
   ├─ TTS: edge-tts (INTERNET) ili Piper (lokalno)
   └─ ffmpeg filteri → soxr 8 kHz → normalizacija → 20 ms okviri → nazad u Asterisk
```

Ključna ideja [VIDLJIVO U KODU]: LLM ne vodi razgovor slobodno. On samo izvlači JSON (`intent`, `date_iso`, `time`, `name`, `phone`, `confirm`), a odluku šta se odgovara donosi Python kod (`next_reply`). Zato je ponašanje predvidljivo. Dodatni slojevi sigurnosti: `sanitize_user_text` (300 znakova, bez kontrolnih znakova), whitelist `ALLOWED_INTENTS`, `validate_name/phone/time`, deterministički parser vremena `parse_serbian_time_fallback`, zabrana termina u prošlosti (`MIN_LEAD_MINUTES=30`).

AudioSocket protokol [VIDLJIVO U KODU + Asterisk dokumentacija]: svaki paket = 1 bajt tip + 2 bajta dužina (big-endian) + payload. `0x01` = 16 bajta UUID poziva (prvi paket), `0x10` = audio, `0x00` = prekid/hangup, `0xFF` = greška. Okvir je 320 bajta = 20 ms @ 8 kHz, 16-bit mono.

Zašto `Player` šalje tišinu: Asterisk očekuje neprekidan tok. `Player._run` svakih 20 ms (apsolutno merenje vremena, bez drifta) šalje ili govor ili tišinu. To je glavni razlog što je zvuk u v2 adapteru čist.

## 2. Šta sve mora da radi (redosled pokretanja od nule)

Portovi: llama-server `8082`, whisper-server `8081`, AudioSocket adapter `9092`, SIP `5060`.

**Korak A – sistemski paketi (Ubuntu)**

```bash
sudo apt update
sudo apt install -y asterisk ffmpeg git python3-venv python3-pip build-essential cmake curl
```

Proveri da je modul učitan: `sudo asterisk -rx "module show like audiosocket"` (treba `app_audiosocket` i `res_audiosocket`). Ako ga nema, tvoj Asterisk paket nema AudioSocket i poziv na 601 neće raditi.

**Korak B – repo i venv** (u README: `~/local-voice/venv`)

```bash
git clone https://github.com/masterbg94/AIVA-Master.git ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
pip install edge-tts piper-tts          # edge-tts: opciono ali default; piper: lokalni TTS
pip install google-api-python-client google-auth   # SAMO ako koristiš Google Calendar
```

Adapter i agent koriste uglavnom standardnu biblioteku. Spoljni alati koje zovu: `ffmpeg` (obavezno, mora imati soxr za najbolji resampling; ako nema, kod sam pada na običan `aresample`), `piper` (ako `AIVA_TTS=piper`). `pw-play` i `pw-record` trebaju samo za lokalni mikrofon režim `sim_zubar*.py`, ne za adapter.

**Korak C – Piper model**
Kod očekuje `~/models/piper/sr_Marko_medium.onnx` (uz njega mora ići .onnx.json). Provera: `ls ~/models/piper/`.

**Korak D – llama.cpp (LLM, port 8082)** iz [readme2.md]

```bash
cd ~/llama.cpp && ./build/bin/llama-server \
  -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M \
  --device Vulkan0 -ngl 99 --host 127.0.0.1 --port 8082 -c 4096
```

NAPOMENA: u `readme.md` ista komanda je napisana u više redova BEZ `\` – ako je kopiraš tako, shell će je pokvariti. Koristi jednolinijsku verziju iz `readme2.md` ili ovu sa `\`. Provera: `curl -s http://127.0.0.1:8082/health`.

**Korak E – whisper.cpp (STT, port 8081)** iz [readme2.md]

```bash
cd ~/whisper.cpp/build/bin && ./whisper-server -m ../../models/ggml-large-v3-turbo-q5_0.bin \
  --host 127.0.0.1 --port 8081 -l sr -t 4
```

Provera: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/` (očekuj 200).

**Korak F – Asterisk**

```bash
sudo systemctl enable --now asterisk
sudo asterisk -rvvv                    # konzola
```

U konzoli: `pjsip show endpoints`, `pjsip show contacts` (Linphone mora biti registrovan), `dialplan show`.

**Korak G – Python adapter (test režim, bez pravih rezervacija)**

```bash
cd ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Treba da vidiš: `[SIP] Slušam na 127.0.0.1:9092; test režim, bez rezervacija; TTS=piper`. Za prave rezervacije dodaj `AIVA_LIVE_BOOKING=1`.

**Korak H – poziv**: u Linphone-u (registrovan na Asterisk) pozovi `601`.

VAŽNO o TTS-u [VIDLJIVO U KODU]: podrazumevano je `AIVA_TTS=edge`, što šalje tekst Microsoft servisu (potreban internet; nije lokalno). Za potpuno lokalni rad koristi `AIVA_TTS=piper`. Takođe, `main()` u adapteru na startu radi `warm_cache` bez try/except – ako TTS na startu ne uspe, adapter se ruši pre nego što počne da sluša.

Podešavanja okruženja (adapter): `AIVA_RMS_THRESHOLD` (250), `AIVA_END_SILENCE_MS` (900), `AIVA_MAX_TURNS` (20), `AIVA_TTS`, `AIVA_PIPER_CYR`, `AIVA_TARGET_RMS` (2600), `AIVA_PEAK` (26000), `AIVA_LIVE_BOOKING`, `AIVA_DUMP_DIR`, `AIVA_PIPER_EXTRA`. Agent: `WHISPER_URL`, `LLM_URL`, `LLM_PROVIDER` (local|nvidia), `GOOGLE_SA_FILE`, `GOOGLE_CALENDAR_ID`, `OWNER_PHONE_NUMBER`.

## 3. TODO 1 – Asterisk komande koje ti trebaju

```bash
sudo asterisk -rvvv                      # uđi u CLI (v = verbose)
sudo asterisk -rx "core show channels"   # aktivni pozivi (jedna komanda bez ulaska u CLI)
sudo asterisk -rx "pjsip show endpoints" # endpointi (601)
sudo asterisk -rx "pjsip show contacts"  # da li je Linphone registrovan i sa koje IP
sudo asterisk -rx "dialplan show"        # ceo dialplan
sudo asterisk -rx "dialplan reload"      # posle izmene extensions.conf
sudo asterisk -rx "module reload res_pjsip.so"  # posle izmene pjsip.conf (ili: core reload)
sudo asterisk -rx "module show like audiosocket"
sudo asterisk -rx "core set verbose 5"
sudo asterisk -rx "pjsip set logger on"  # SIP poruke u konzoli (za debug registracije)
sudo ss -ltnp | grep 9092                # da li adapter sluša
sudo journalctl -u asterisk -f           # sistemski log servisa
```

Šablon dialplan-a za 601 [ZAKLJUČAK iz koda adaptera + Asterisk dokumentacije; tvoj stvarni fajl proveri sa `sudo grep -n -B2 -A8 -i audiosocket /etc/asterisk/extensions.conf`]:

```
; context mora biti ISTI kao context= u endpointu 601 u pjsip.conf
[from-internal]
exten => 601,1,Answer()
 same => n,Set(CALLUUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,AudioSocket(${CALLUUID},127.0.0.1:9092)
 same => n,Hangup()
```

AudioSocket traži UUID u standardnom obliku 8-4-4-4-12; `${UNIQUEID}` NIJE validan. Adapter proverava samo da je prvi paket tipa `0x01` sa tačno 16 bajta, pa za jedan poziv radi i fiksni UUID, npr. `AudioSocket(11111111-2222-3333-4444-555555555555,127.0.0.1:9092)`. Ako `SHELL()` nije dostupan: `sudo asterisk -rx "core show function SHELL"`.

Šablon PJSIP endpointa (iz Asterisk dokumentacije; lozinke zameni):

```
[601]
type=endpoint
context=from-internal
disallow=all
allow=ulaw
allow=alaw
auth=auth601
aors=601

[auth601]
type=auth
auth_type=userpass
username=601
password=PROMENI_ME

[601]
type=aor
max_contacts=1
```

Codec `ulaw/alaw` je 8 kHz, što se poklapa sa 8 kHz tokom koji adapter očekuje.

## 4. TODO 3 – Kako pokrenuti `sim_zubar8_fixed.py` umesto `asterisk_adapter2.py`

Činjenice:

- `asterisk_adapter2.py` ima tvrdo upisano `import sim_zubar7 as agent`. Znači, adapter NE koristi `fixed`.
- Vidljivi deo `sim_zubar8_fixed.py` je isti tip fajla kao `sim_zubar7` (lokalni režim: `pw-record` mikrofon + `pw-play`, `input()` ENTER). Docstring i dalje kaže `sim_zubar7.py`. Jedina vidljiva razlika: `OWNER_PHONE_NUMBER` podrazumevano `""` (u `sim_zubar7/8` je upisan stvaran broj).
- `readme2.md` (V2) pokreće `fixed` sa `AIVA_TTS=piper AIVA_LIVE_BOOKING=0`. To su baš promenljive adaptera. Fajl `fixed` je 60 KB naspram 41 KB (razlika ≈ 19 KB ≈ veličina adaptera od 21 KB).
- [ZAKLJUČAK, NIJE POTVRĐENO]: najverovatnije je u `fixed` spojen adapter u isti fajl (jedan proces: AudioSocket server + agent). Nisam mogao da vidim rep fajla.

Proveri ovo PRE bilo čega (lokalno, u repou):

```bash
python3 -m py_compile sim_zubar8_fixed.py && echo SINTAKSA_OK
grep -n "9092\|start_server\|AIVA_\|__main__\|def next_reply\|def main" sim_zubar8_fixed.py
```

**Scenario A** – grep pokazuje `start_server`, `9092`, `AIVA_*`, `__main__`: fajl je samostalan. Pokretanje:

```bash
LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0 python3 -u ./sim_zubar8_fixed.py
```

Asterisk se ne dira ako i dalje sluša na `127.0.0.1:9092`. Ne pokreći ga istovremeno sa `asterisk_adapter2.py` (isti port → `Address already in use`).

**Scenario B** – nema `start_server`/`9092`: `fixed` je samo agent modul. Tada ga koristi adapter. Najčistija izmena u `asterisk_adapter2.py` (umesto `import sim_zubar7 as agent`):

```python
import importlib, os
agent = importlib.import_module(os.getenv("AIVA_AGENT", "sim_zubar7"))
```

pa:

```bash
AIVA_AGENT=sim_zubar8_fixed AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Uslov: modul mora da ima ono što adapter koristi: `WORK_DAYS`, `DAILY_SLOTS`, `is_slot_in_future`, `CalendarManager`, `extract` (adapter prihvata 2 ili 3 parametra preko `inspect`), `transcribe`, `normalize_phone`, `validate_name`, `next_reply(info, state, text, calendar)` koji vraća trojku `(reply, done, transfer)`, `_generate_edge_tts_wav`, `_generate_piper_wav`, `latin_to_cyrillic`, `PIPER_MODEL`.

## 5. TODO 4 – Log u terminalu (šta si rekao na telefon)

Adapter to VEĆ radi [VIDLJIVO U KODU], ali samo ako ga pokreneš u foreground terminalu sa `python3 -u`:

- `[SIP] Novi poziv` – Asterisk se povezao.
- `[VAD] Replika: 3.2 s` – detektovan govor.
- `[ASR] 'može sutra u deset i trideset'` – šta je Whisper razumeo (to je ono što tražiš).
- `[LLM] {...}` – izvučeni JSON.
- `[STATE] {...}` – stanje razgovora (ime, telefon, datum…).
- `[SIP] Poziv završen`.

Ako vidiš samo `Slušam na 127.0.0.1:9092` a ništa pri pozivu: poziv ne stiže do adaptera → problem je u dialplanu/modulu (`module show like audiosocket`) ili je adapter zauzet (`busy`).

Dodatni dijagnostički alati:

- `AIVA_DUMP_DIR=/tmp/aiva` snima PCM koji se šalje ka telefonu; preslušaj: `ffplay -f s16le -ar 8000 -ac 1 /tmp/aiva/<fajl>.raw`.
- Asterisk strana: `sudo asterisk -rvvv` + `core set verbose 5`.

Privatnost: `[ASR]` i `[STATE]` ispisuju ime i telefon pacijenta u čistom tekstu. Agent ima `mask_pii()` ali ga adapter ne koristi. Za test je OK; za produkciju stavi iza env prekidača (npr. `AIVA_LOG_ASR=1`).

## 6. TODO 5 – Da li menjati Asterisk config kad menjam Python skriptu?

Kratko: NE, dokle god nova skripta sluša na istoj adresi/portu i priča AudioSocket protokolom.

Moraš da menjaš `extensions.conf` samo ako: (1) menjaš port ili host (`127.0.0.1:9092`); (2) nova skripta ne koristi AudioSocket (npr. ARI/ExternalMedia ili lokalni mikrofon režim) – tada poziv sa 601 ne može da je dosegne; (3) dodaješ nove test brojeve (899, 600).

`pjsip.conf` ne diraš zbog promene Python programa – on se tiče samo SIP registracije (601, lozinka, codec, transport). Menjaš ga samo ako se menja uređaj/mreža/IP.

Posle svake izmene:

```bash
sudoedit /etc/asterisk/extensions.conf
sudo asterisk -rx "dialplan reload"
sudoedit /etc/asterisk/pjsip.conf
sudo asterisk -rx "module reload res_pjsip.so"
```

Savet: pre izmene napravi kopiju: `sudo cp /etc/asterisk/extensions.conf /etc/asterisk/extensions.conf.bak`.

## 7. TODO 6 – Test brojevi 899 i 600

**899 – eho test (mreža + codec + Linphone)**. Tvoj primer je ispravan. Predlog sa jasnijim tokom:

```
exten => 899,1,Answer()
 same => n,Wait(1)
 same => n,Playback(demo-echotest)
 same => n,Echo()
 same => n,Playback(demo-echodone)
 same => n,Hangup()
```

`demo-echotest` je u zvaničnom primeru `extensions.conf.sample`. Proveri da zvukovi postoje: `ls /var/lib/asterisk/sounds/en/ | grep demo-`. Ako ne, ostavi `demo-congrats` kao u tvom primeru.

Ograničenje: Echo meri samo put Linphone ↔ Asterisk. Ne testira kvalitet AI glasa. Ako je eho čist, a AI glas šušti, problem je u adapteru/TTS-u, ne u mreži.

**898 (predlog) – isti TTS zvuk bez AI-a**: napravi fajl u istom formatu koji adapter šalje i pusti ga iz Asteriska.

```bash
sudo mkdir -p /var/lib/asterisk/sounds/custom
ffmpeg -i neki_tts.wav -af "highpass=f=90,lowpass=f=3500" -ar 8000 -ac 1 -c:a pcm_s16le aiva-test.wav
sudo mv aiva-test.wav /var/lib/asterisk/sounds/custom/ && sudo chown asterisk:asterisk /var/lib/asterisk/sounds/custom/aiva-test.wav
```

```
exten => 898,1,Answer()
 same => n,Playback(custom/aiva-test)
 same => n,Hangup()
```

Tumačenje: 898 čist + AI poziv šušti → greška u slanju okvira (CPU zastoji, `Player` sat). 898 takođe šušti → codec/mreža/Linphone.

**600 – provera da je sve pokrenuto**. Dialplan sam ne može da proveri Whisper/LLM, pa pozove skriptu. Napravi `/usr/local/bin/aiva-health.sh`:

```bash
#!/bin/bash
ok=0
curl -s -m 2 -o /dev/null http://127.0.0.1:8081/ && ok=$((ok+1))
curl -s -m 2 http://127.0.0.1:8082/health | grep -q ok && ok=$((ok+1))
ss -ltn | grep -q ':9092 ' && ok=$((ok+1))
command -v ffmpeg >/dev/null && ok=$((ok+1))
echo -n $ok
```

```bash
sudo chmod +x /usr/local/bin/aiva-health.sh
```

```
exten => 600,1,Answer()
 same => n,Set(N=${SHELL(/usr/local/bin/aiva-health.sh)})
 same => n,GotoIf($["${N}" = "4"]?ok:bad)
 same => n(ok),Playback(beep)
 same => n,SayNumber(${N})
 same => n,Hangup()
 same => n(bad),SayNumber(${N})
 same => n,Playback(beep)
 same => n,Hangup()
```

Značenje: izgovorena cifra 4 = svih 4 provere prošle (Whisper, LLM, adapter na 9092, ffmpeg). Isti skript možeš pokrenuti i u terminalu (`/usr/local/bin/aiva-health.sh`) – to je pouzdanija prva provera. Ovaj 600 dialplan nije testiran na tvom sistemu; ako `SHELL()` nije učitan, koristi samo terminalnu verziju.

Ne zaboravi `sudo asterisk -rx "dialplan reload"` i da brojevi budu u istom contextu kao 601.

## 8. Code review – nalazi po prioritetu

**P0 – proveri odmah**

- [VIDLJIVO U KODU] U `sim_zubar7.py`, `sim_zubar8.py` i `sim_zubar8_fixed.py` postoje zakomentarisani redovi `# def http_json(...)` i `# def llm(...)` čije telo NIJE zakomentarisano (uvučene linije). Po mom čitanju Python bi to odbio sa `IndentationError: unexpected indent`, pa `import sim_zubar7` u adapteru ne bi prošao. Ako lokalno radi, onda se lokalna kopija razlikuje od GitHub-a. Provera: `python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py`. Popravka: obriši sve zakomentarisane stare verzije (ostaje po jedna `def http_json` i `def llm`).
- `sim_zubar7.py` i `sim_zubar8.py` imaju identičan SHA (`ab4f72e8…`, 41081 B) – dupli fajl. Obriši jedan.
- Ličnost/privatnost: u `sim_zubar7/8` je javni repo sa upisanim `OWNER_PHONE_NUMBER="+381640877180"`. Premesti u env i obriši iz istorije ako ne želiš da je javno. `osmeh_bookings.db` i `__pycache__/` su commitovani – dodaj u `.gitignore` (baza može sadržati podatke pacijenata).

**P1 – logika**

- `CalendarManager.book_slot`: upis u SQLite se commit-uje PRE provere/upisa u Google Calendar. Ako je slot zauzet u Google-u ili `events().insert` baci grešku, u SQLite ostaje fantomski termin koji blokira slot. Rešenje: upis u Google prvo, pa SQLite; ili `DELETE` u `except`/pri `return False`.
- `sqlite3.connect()` kao context manager radi commit/rollback, ali NE zatvara konekciju – koristi `contextlib.closing`.
- `validate_name` zahteva razmak (ime + prezime); jedno ime se odbija.
- `parse_serbian_time_fallback` pokriva samo vaših 5 slotova; `"devet"` u „devet i trideset” vraća 09:00.
- Adapter: `warm_cache` bez try/except; jedan poziv istovremeno (`busy`); nema barge-in (korisnik ne može da prekine asistenta); docstring adaptera kaže `asterisk_adapter.py` i `sim_zubar7` – zastareo.
- `LLM_PROVIDER=nvidia` šalje transkript pacijenta u oblak; naziv modela `nvidia/nemotron-3.5-bolt-e4b` nisam mogao da potvrdim – proveri u NVIDIA katalogu.

**P2 – higijena**

- 8 verzija `sim_zubar*.py` u korenu: premesti stare u `archive/` ili koristi git tagove.
- Dodaj `requirements.txt` i `run.sh` sa env promenljivama.
- Dodaj `pytest` testove za čiste funkcije (`parse_serbian_time_fallback`, `normalize_phone`, `spoken_phone`, `cyr_to_lat`, `split_sentences`, `next_reply` sa lažnim kalendarom).
- Pokretanje kao `systemd` servisi (llama, whisper, adapter) kada test prođe.

## 9. Šta ostaje da potvrdiš (da bi bilo 100%)

Pošalji izlaz ovih komandi i dovršiću analizu bez nagađanja:

```bash
cd ~/Documents/Workspace/test
python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py asterisk_adapter2.py; echo exit=$?
wc -c sim_zubar7.py sim_zubar8_fixed.py
grep -n "def next_reply\|start_server\|9092\|AIVA_\|__main__" sim_zubar8_fixed.py
sudo grep -n -B2 -A8 -i "audiosocket\|601" /etc/asterisk/extensions.conf
sudo grep -n -A10 "\[601\]" /etc/asterisk/pjsip.conf
sudo asterisk -rx "module show like audiosocket"
```

Brzina odziva (Whisper turbo + Qwen3-4B na Vulkan + TTS po replici) nije merena; ne mogu da tvrdim latenciju bez tvog logova.## 0. Šta je pregledano, a šta NIJE (iskreno)

Pročitano u celosti: struktura repoa, `readme.md`, `readme2.md`, `todo.md`, `asterisk_adapter2.py`.

Pročitano delimično: `sim_zubar7.py`, `sim_zubar8_fixed.py`. GitHub konektor je odsekao sadržaj posle `extract()` prompta (otprilike prvih 2/3 `sim_zubar7`, još manje `fixed`). Nisam video: `next_reply()`, dijaloški state machine, glavnu petlju i sve što je u `fixed` iznad 41 KB.

Nije čitano: `sim_test.py`, `sim_zubar.py`–`sim_zubar5.py`, `my_voice_test.py`, `omniVoiceTest/`, `osmeh_bookings.db` (binarno), `.vscode/`.

Posebno: Asterisk konfiguracija (`/etc/asterisk/*.conf`) NIJE u repou. Zato su dole primeri šabloni, a ne tvoj stvarni config. Svaka tvrdnja je označena kao [VIDLJIVO U KODU], [ZAKLJUČAK] ili [PROVERI KOMANDOM].

## 1. Arhitektura – kako sve radi

```
Linphone (601)
   │ SIP/RTP (ulaw/alaw, 8 kHz)
   ▼
Asterisk (PJSIP + dialplan)
   │ AudioSocket(uuid,127.0.0.1:9092)  ← TCP, slin 8 kHz, 16-bit, mono
   ▼
asterisk_adapter2.py  (Python asyncio server)
   ├─ VAD (RMS prag) → ceo iskaz pacijenta
   ├─ ffmpeg → 16 kHz WAV → Whisper server  :8081 (/inference)  = STT
   ├─ sim_zubar7.extract() → llama-server   :8082 (/v1/chat/completions) = JSON intent
   ├─ sim_zubar7.next_reply() = deterministički state machine zakazivanja
   ├─ TTS: edge-tts (INTERNET) ili Piper (lokalno)
   └─ ffmpeg filteri → soxr 8 kHz → normalizacija → 20 ms okviri → nazad u Asterisk
```

Ključna ideja [VIDLJIVO U KODU]: LLM ne vodi razgovor slobodno. On samo izvlači JSON (`intent`, `date_iso`, `time`, `name`, `phone`, `confirm`), a odluku šta se odgovara donosi Python kod (`next_reply`). Zato je ponašanje predvidljivo. Dodatni slojevi sigurnosti: `sanitize_user_text` (300 znakova, bez kontrolnih znakova), whitelist `ALLOWED_INTENTS`, `validate_name/phone/time`, deterministički parser vremena `parse_serbian_time_fallback`, zabrana termina u prošlosti (`MIN_LEAD_MINUTES=30`).

AudioSocket protokol [VIDLJIVO U KODU + Asterisk dokumentacija]: svaki paket = 1 bajt tip + 2 bajta dužina (big-endian) + payload. `0x01` = 16 bajta UUID poziva (prvi paket), `0x10` = audio, `0x00` = prekid/hangup, `0xFF` = greška. Okvir je 320 bajta = 20 ms @ 8 kHz, 16-bit mono.

Zašto `Player` šalje tišinu: Asterisk očekuje neprekidan tok. `Player._run` svakih 20 ms (apsolutno merenje vremena, bez drifta) šalje ili govor ili tišinu. To je glavni razlog što je zvuk u v2 adapteru čist.

## 2. Šta sve mora da radi (redosled pokretanja od nule)

Portovi: llama-server `8082`, whisper-server `8081`, AudioSocket adapter `9092`, SIP `5060`.

**Korak A – sistemski paketi (Ubuntu)**

```bash
sudo apt update
sudo apt install -y asterisk ffmpeg git python3-venv python3-pip build-essential cmake curl
```

Proveri da je modul učitan: `sudo asterisk -rx "module show like audiosocket"` (treba `app_audiosocket` i `res_audiosocket`). Ako ga nema, tvoj Asterisk paket nema AudioSocket i poziv na 601 neće raditi.

**Korak B – repo i venv** (u README: `~/local-voice/venv`)

```bash
git clone https://github.com/masterbg94/AIVA-Master.git ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
pip install edge-tts piper-tts          # edge-tts: opciono ali default; piper: lokalni TTS
pip install google-api-python-client google-auth   # SAMO ako koristiš Google Calendar
```

Adapter i agent koriste uglavnom standardnu biblioteku. Spoljni alati koje zovu: `ffmpeg` (obavezno, mora imati soxr za najbolji resampling; ako nema, kod sam pada na običan `aresample`), `piper` (ako `AIVA_TTS=piper`). `pw-play` i `pw-record` trebaju samo za lokalni mikrofon režim `sim_zubar*.py`, ne za adapter.

**Korak C – Piper model**
Kod očekuje `~/models/piper/sr_Marko_medium.onnx` (uz njega mora ići .onnx.json). Provera: `ls ~/models/piper/`.

**Korak D – llama.cpp (LLM, port 8082)** iz [readme2.md]

```bash
cd ~/llama.cpp && ./build/bin/llama-server \
  -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M \
  --device Vulkan0 -ngl 99 --host 127.0.0.1 --port 8082 -c 4096
```

NAPOMENA: u `readme.md` ista komanda je napisana u više redova BEZ `\` – ako je kopiraš tako, shell će je pokvariti. Koristi jednolinijsku verziju iz `readme2.md` ili ovu sa `\`. Provera: `curl -s http://127.0.0.1:8082/health`.

**Korak E – whisper.cpp (STT, port 8081)** iz [readme2.md]

```bash
cd ~/whisper.cpp/build/bin && ./whisper-server -m ../../models/ggml-large-v3-turbo-q5_0.bin \
  --host 127.0.0.1 --port 8081 -l sr -t 4
```

Provera: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/` (očekuj 200).

**Korak F – Asterisk**

```bash
sudo systemctl enable --now asterisk
sudo asterisk -rvvv                    # konzola
```

U konzoli: `pjsip show endpoints`, `pjsip show contacts` (Linphone mora biti registrovan), `dialplan show`.

**Korak G – Python adapter (test režim, bez pravih rezervacija)**

```bash
cd ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Treba da vidiš: `[SIP] Slušam na 127.0.0.1:9092; test režim, bez rezervacija; TTS=piper`. Za prave rezervacije dodaj `AIVA_LIVE_BOOKING=1`.

**Korak H – poziv**: u Linphone-u (registrovan na Asterisk) pozovi `601`.

VAŽNO o TTS-u [VIDLJIVO U KODU]: podrazumevano je `AIVA_TTS=edge`, što šalje tekst Microsoft servisu (potreban internet; nije lokalno). Za potpuno lokalni rad koristi `AIVA_TTS=piper`. Takođe, `main()` u adapteru na startu radi `warm_cache` bez try/except – ako TTS na startu ne uspe, adapter se ruši pre nego što počne da sluša.

Podešavanja okruženja (adapter): `AIVA_RMS_THRESHOLD` (250), `AIVA_END_SILENCE_MS` (900), `AIVA_MAX_TURNS` (20), `AIVA_TTS`, `AIVA_PIPER_CYR`, `AIVA_TARGET_RMS` (2600), `AIVA_PEAK` (26000), `AIVA_LIVE_BOOKING`, `AIVA_DUMP_DIR`, `AIVA_PIPER_EXTRA`. Agent: `WHISPER_URL`, `LLM_URL`, `LLM_PROVIDER` (local|nvidia), `GOOGLE_SA_FILE`, `GOOGLE_CALENDAR_ID`, `OWNER_PHONE_NUMBER`.

## 3. TODO 1 – Asterisk komande koje ti trebaju

```bash
sudo asterisk -rvvv                      # uđi u CLI (v = verbose)
sudo asterisk -rx "core show channels"   # aktivni pozivi (jedna komanda bez ulaska u CLI)
sudo asterisk -rx "pjsip show endpoints" # endpointi (601)
sudo asterisk -rx "pjsip show contacts"  # da li je Linphone registrovan i sa koje IP
sudo asterisk -rx "dialplan show"        # ceo dialplan
sudo asterisk -rx "dialplan reload"      # posle izmene extensions.conf
sudo asterisk -rx "module reload res_pjsip.so"  # posle izmene pjsip.conf (ili: core reload)
sudo asterisk -rx "module show like audiosocket"
sudo asterisk -rx "core set verbose 5"
sudo asterisk -rx "pjsip set logger on"  # SIP poruke u konzoli (za debug registracije)
sudo ss -ltnp | grep 9092                # da li adapter sluša
sudo journalctl -u asterisk -f           # sistemski log servisa
```

Šablon dialplan-a za 601 [ZAKLJUČAK iz koda adaptera + Asterisk dokumentacije; tvoj stvarni fajl proveri sa `sudo grep -n -B2 -A8 -i audiosocket /etc/asterisk/extensions.conf`]:

```
; context mora biti ISTI kao context= u endpointu 601 u pjsip.conf
[from-internal]
exten => 601,1,Answer()
 same => n,Set(CALLUUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,AudioSocket(${CALLUUID},127.0.0.1:9092)
 same => n,Hangup()
```

AudioSocket traži UUID u standardnom obliku 8-4-4-4-12; `${UNIQUEID}` NIJE validan. Adapter proverava samo da je prvi paket tipa `0x01` sa tačno 16 bajta, pa za jedan poziv radi i fiksni UUID, npr. `AudioSocket(11111111-2222-3333-4444-555555555555,127.0.0.1:9092)`. Ako `SHELL()` nije dostupan: `sudo asterisk -rx "core show function SHELL"`.

Šablon PJSIP endpointa (iz Asterisk dokumentacije; lozinke zameni):

```
[601]
type=endpoint
context=from-internal
disallow=all
allow=ulaw
allow=alaw
auth=auth601
aors=601

[auth601]
type=auth
auth_type=userpass
username=601
password=PROMENI_ME

[601]
type=aor
max_contacts=1
```

Codec `ulaw/alaw` je 8 kHz, što se poklapa sa 8 kHz tokom koji adapter očekuje.

## 4. TODO 3 – Kako pokrenuti `sim_zubar8_fixed.py` umesto `asterisk_adapter2.py`

Činjenice:

- `asterisk_adapter2.py` ima tvrdo upisano `import sim_zubar7 as agent`. Znači, adapter NE koristi `fixed`.
- Vidljivi deo `sim_zubar8_fixed.py` je isti tip fajla kao `sim_zubar7` (lokalni režim: `pw-record` mikrofon + `pw-play`, `input()` ENTER). Docstring i dalje kaže `sim_zubar7.py`. Jedina vidljiva razlika: `OWNER_PHONE_NUMBER` podrazumevano `""` (u `sim_zubar7/8` je upisan stvaran broj).
- `readme2.md` (V2) pokreće `fixed` sa `AIVA_TTS=piper AIVA_LIVE_BOOKING=0`. To su baš promenljive adaptera. Fajl `fixed` je 60 KB naspram 41 KB (razlika ≈ 19 KB ≈ veličina adaptera od 21 KB).
- [ZAKLJUČAK, NIJE POTVRĐENO]: najverovatnije je u `fixed` spojen adapter u isti fajl (jedan proces: AudioSocket server + agent). Nisam mogao da vidim rep fajla.

Proveri ovo PRE bilo čega (lokalno, u repou):

```bash
python3 -m py_compile sim_zubar8_fixed.py && echo SINTAKSA_OK
grep -n "9092\|start_server\|AIVA_\|__main__\|def next_reply\|def main" sim_zubar8_fixed.py
```

**Scenario A** – grep pokazuje `start_server`, `9092`, `AIVA_*`, `__main__`: fajl je samostalan. Pokretanje:

```bash
LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0 python3 -u ./sim_zubar8_fixed.py
```

Asterisk se ne dira ako i dalje sluša na `127.0.0.1:9092`. Ne pokreći ga istovremeno sa `asterisk_adapter2.py` (isti port → `Address already in use`).

**Scenario B** – nema `start_server`/`9092`: `fixed` je samo agent modul. Tada ga koristi adapter. Najčistija izmena u `asterisk_adapter2.py` (umesto `import sim_zubar7 as agent`):

```python
import importlib, os
agent = importlib.import_module(os.getenv("AIVA_AGENT", "sim_zubar7"))
```

pa:

```bash
AIVA_AGENT=sim_zubar8_fixed AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Uslov: modul mora da ima ono što adapter koristi: `WORK_DAYS`, `DAILY_SLOTS`, `is_slot_in_future`, `CalendarManager`, `extract` (adapter prihvata 2 ili 3 parametra preko `inspect`), `transcribe`, `normalize_phone`, `validate_name`, `next_reply(info, state, text, calendar)` koji vraća trojku `(reply, done, transfer)`, `_generate_edge_tts_wav`, `_generate_piper_wav`, `latin_to_cyrillic`, `PIPER_MODEL`.

## 5. TODO 4 – Log u terminalu (šta si rekao na telefon)

Adapter to VEĆ radi [VIDLJIVO U KODU], ali samo ako ga pokreneš u foreground terminalu sa `python3 -u`:

- `[SIP] Novi poziv` – Asterisk se povezao.
- `[VAD] Replika: 3.2 s` – detektovan govor.
- `[ASR] 'može sutra u deset i trideset'` – šta je Whisper razumeo (to je ono što tražiš).
- `[LLM] {...}` – izvučeni JSON.
- `[STATE] {...}` – stanje razgovora (ime, telefon, datum…).
- `[SIP] Poziv završen`.

Ako vidiš samo `Slušam na 127.0.0.1:9092` a ništa pri pozivu: poziv ne stiže do adaptera → problem je u dialplanu/modulu (`module show like audiosocket`) ili je adapter zauzet (`busy`).

Dodatni dijagnostički alati:

- `AIVA_DUMP_DIR=/tmp/aiva` snima PCM koji se šalje ka telefonu; preslušaj: `ffplay -f s16le -ar 8000 -ac 1 /tmp/aiva/<fajl>.raw`.
- Asterisk strana: `sudo asterisk -rvvv` + `core set verbose 5`.

Privatnost: `[ASR]` i `[STATE]` ispisuju ime i telefon pacijenta u čistom tekstu. Agent ima `mask_pii()` ali ga adapter ne koristi. Za test je OK; za produkciju stavi iza env prekidača (npr. `AIVA_LOG_ASR=1`).

## 6. TODO 5 – Da li menjati Asterisk config kad menjam Python skriptu?

Kratko: NE, dokle god nova skripta sluša na istoj adresi/portu i priča AudioSocket protokolom.

Moraš da menjaš `extensions.conf` samo ako: (1) menjaš port ili host (`127.0.0.1:9092`); (2) nova skripta ne koristi AudioSocket (npr. ARI/ExternalMedia ili lokalni mikrofon režim) – tada poziv sa 601 ne može da je dosegne; (3) dodaješ nove test brojeve (899, 600).

`pjsip.conf` ne diraš zbog promene Python programa – on se tiče samo SIP registracije (601, lozinka, codec, transport). Menjaš ga samo ako se menja uređaj/mreža/IP.

Posle svake izmene:

```bash
sudoedit /etc/asterisk/extensions.conf
sudo asterisk -rx "dialplan reload"
sudoedit /etc/asterisk/pjsip.conf
sudo asterisk -rx "module reload res_pjsip.so"
```

Savet: pre izmene napravi kopiju: `sudo cp /etc/asterisk/extensions.conf /etc/asterisk/extensions.conf.bak`.

## 7. TODO 6 – Test brojevi 899 i 600

**899 – eho test (mreža + codec + Linphone)**. Tvoj primer je ispravan. Predlog sa jasnijim tokom:

```
exten => 899,1,Answer()
 same => n,Wait(1)
 same => n,Playback(demo-echotest)
 same => n,Echo()
 same => n,Playback(demo-echodone)
 same => n,Hangup()
```

`demo-echotest` je u zvaničnom primeru `extensions.conf.sample`. Proveri da zvukovi postoje: `ls /var/lib/asterisk/sounds/en/ | grep demo-`. Ako ne, ostavi `demo-congrats` kao u tvom primeru.

Ograničenje: Echo meri samo put Linphone ↔ Asterisk. Ne testira kvalitet AI glasa. Ako je eho čist, a AI glas šušti, problem je u adapteru/TTS-u, ne u mreži.

**898 (predlog) – isti TTS zvuk bez AI-a**: napravi fajl u istom formatu koji adapter šalje i pusti ga iz Asteriska.

```bash
sudo mkdir -p /var/lib/asterisk/sounds/custom
ffmpeg -i neki_tts.wav -af "highpass=f=90,lowpass=f=3500" -ar 8000 -ac 1 -c:a pcm_s16le aiva-test.wav
sudo mv aiva-test.wav /var/lib/asterisk/sounds/custom/ && sudo chown asterisk:asterisk /var/lib/asterisk/sounds/custom/aiva-test.wav
```

```
exten => 898,1,Answer()
 same => n,Playback(custom/aiva-test)
 same => n,Hangup()
```

Tumačenje: 898 čist + AI poziv šušti → greška u slanju okvira (CPU zastoji, `Player` sat). 898 takođe šušti → codec/mreža/Linphone.

**600 – provera da je sve pokrenuto**. Dialplan sam ne može da proveri Whisper/LLM, pa pozove skriptu. Napravi `/usr/local/bin/aiva-health.sh`:

```bash
#!/bin/bash
ok=0
curl -s -m 2 -o /dev/null http://127.0.0.1:8081/ && ok=$((ok+1))
curl -s -m 2 http://127.0.0.1:8082/health | grep -q ok && ok=$((ok+1))
ss -ltn | grep -q ':9092 ' && ok=$((ok+1))
command -v ffmpeg >/dev/null && ok=$((ok+1))
echo -n $ok
```

```bash
sudo chmod +x /usr/local/bin/aiva-health.sh
```

```
exten => 600,1,Answer()
 same => n,Set(N=${SHELL(/usr/local/bin/aiva-health.sh)})
 same => n,GotoIf($["${N}" = "4"]?ok:bad)
 same => n(ok),Playback(beep)
 same => n,SayNumber(${N})
 same => n,Hangup()
 same => n(bad),SayNumber(${N})
 same => n,Playback(beep)
 same => n,Hangup()
```

Značenje: izgovorena cifra 4 = svih 4 provere prošle (Whisper, LLM, adapter na 9092, ffmpeg). Isti skript možeš pokrenuti i u terminalu (`/usr/local/bin/aiva-health.sh`) – to je pouzdanija prva provera. Ovaj 600 dialplan nije testiran na tvom sistemu; ako `SHELL()` nije učitan, koristi samo terminalnu verziju.

Ne zaboravi `sudo asterisk -rx "dialplan reload"` i da brojevi budu u istom contextu kao 601.

## 8. Code review – nalazi po prioritetu

**P0 – proveri odmah**

- [VIDLJIVO U KODU] U `sim_zubar7.py`, `sim_zubar8.py` i `sim_zubar8_fixed.py` postoje zakomentarisani redovi `# def http_json(...)` i `# def llm(...)` čije telo NIJE zakomentarisano (uvučene linije). Po mom čitanju Python bi to odbio sa `IndentationError: unexpected indent`, pa `import sim_zubar7` u adapteru ne bi prošao. Ako lokalno radi, onda se lokalna kopija razlikuje od GitHub-a. Provera: `python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py`. Popravka: obriši sve zakomentarisane stare verzije (ostaje po jedna `def http_json` i `def llm`).
- `sim_zubar7.py` i `sim_zubar8.py` imaju identičan SHA (`ab4f72e8…`, 41081 B) – dupli fajl. Obriši jedan.
- Ličnost/privatnost: u `sim_zubar7/8` je javni repo sa upisanim `OWNER_PHONE_NUMBER="+381640877180"`. Premesti u env i obriši iz istorije ako ne želiš da je javno. `osmeh_bookings.db` i `__pycache__/` su commitovani – dodaj u `.gitignore` (baza može sadržati podatke pacijenata).

**P1 – logika**

- `CalendarManager.book_slot`: upis u SQLite se commit-uje PRE provere/upisa u Google Calendar. Ako je slot zauzet u Google-u ili `events().insert` baci grešku, u SQLite ostaje fantomski termin koji blokira slot. Rešenje: upis u Google prvo, pa SQLite; ili `DELETE` u `except`/pri `return False`.
- `sqlite3.connect()` kao context manager radi commit/rollback, ali NE zatvara konekciju – koristi `contextlib.closing`.
- `validate_name` zahteva razmak (ime + prezime); jedno ime se odbija.
- `parse_serbian_time_fallback` pokriva samo vaših 5 slotova; `"devet"` u „devet i trideset” vraća 09:00.
- Adapter: `warm_cache` bez try/except; jedan poziv istovremeno (`busy`); nema barge-in (korisnik ne može da prekine asistenta); docstring adaptera kaže `asterisk_adapter.py` i `sim_zubar7` – zastareo.
- `LLM_PROVIDER=nvidia` šalje transkript pacijenta u oblak; naziv modela `nvidia/nemotron-3.5-bolt-e4b` nisam mogao da potvrdim – proveri u NVIDIA katalogu.

**P2 – higijena**

- 8 verzija `sim_zubar*.py` u korenu: premesti stare u `archive/` ili koristi git tagove.
- Dodaj `requirements.txt` i `run.sh` sa env promenljivama.
- Dodaj `pytest` testove za čiste funkcije (`parse_serbian_time_fallback`, `normalize_phone`, `spoken_phone`, `cyr_to_lat`, `split_sentences`, `next_reply` sa lažnim kalendarom).
- Pokretanje kao `systemd` servisi (llama, whisper, adapter) kada test prođe.

## 9. Šta ostaje da potvrdiš (da bi bilo 100%)
## 0. Šta je pregledano, a šta NIJE (iskreno)

Pročitano u celosti: struktura repoa, `readme.md`, `readme2.md`, `todo.md`, `asterisk_adapter2.py`.

Pročitano delimično: `sim_zubar7.py`, `sim_zubar8_fixed.py`. GitHub konektor je odsekao sadržaj posle `extract()` prompta (otprilike prvih 2/3 `sim_zubar7`, još manje `fixed`). Nisam video: `next_reply()`, dijaloški state machine, glavnu petlju i sve što je u `fixed` iznad 41 KB.

Nije čitano: `sim_test.py`, `sim_zubar.py`–`sim_zubar5.py`, `my_voice_test.py`, `omniVoiceTest/`, `osmeh_bookings.db` (binarno), `.vscode/`.

Posebno: Asterisk konfiguracija (`/etc/asterisk/*.conf`) NIJE u repou. Zato su dole primeri šabloni, a ne tvoj stvarni config. Svaka tvrdnja je označena kao [VIDLJIVO U KODU], [ZAKLJUČAK] ili [PROVERI KOMANDOM].

## 1. Arhitektura – kako sve radi

```
Linphone (601)
   │ SIP/RTP (ulaw/alaw, 8 kHz)
   ▼
Asterisk (PJSIP + dialplan)
   │ AudioSocket(uuid,127.0.0.1:9092)  ← TCP, slin 8 kHz, 16-bit, mono
   ▼
asterisk_adapter2.py  (Python asyncio server)
   ├─ VAD (RMS prag) → ceo iskaz pacijenta
   ├─ ffmpeg → 16 kHz WAV → Whisper server  :8081 (/inference)  = STT
   ├─ sim_zubar7.extract() → llama-server   :8082 (/v1/chat/completions) = JSON intent
   ├─ sim_zubar7.next_reply() = deterministički state machine zakazivanja
   ├─ TTS: edge-tts (INTERNET) ili Piper (lokalno)
   └─ ffmpeg filteri → soxr 8 kHz → normalizacija → 20 ms okviri → nazad u Asterisk
```

Ključna ideja [VIDLJIVO U KODU]: LLM ne vodi razgovor slobodno. On samo izvlači JSON (`intent`, `date_iso`, `time`, `name`, `phone`, `confirm`), a odluku šta se odgovara donosi Python kod (`next_reply`). Zato je ponašanje predvidljivo. Dodatni slojevi sigurnosti: `sanitize_user_text` (300 znakova, bez kontrolnih znakova), whitelist `ALLOWED_INTENTS`, `validate_name/phone/time`, deterministički parser vremena `parse_serbian_time_fallback`, zabrana termina u prošlosti (`MIN_LEAD_MINUTES=30`).

AudioSocket protokol [VIDLJIVO U KODU + Asterisk dokumentacija]: svaki paket = 1 bajt tip + 2 bajta dužina (big-endian) + payload. `0x01` = 16 bajta UUID poziva (prvi paket), `0x10` = audio, `0x00` = prekid/hangup, `0xFF` = greška. Okvir je 320 bajta = 20 ms @ 8 kHz, 16-bit mono.

Zašto `Player` šalje tišinu: Asterisk očekuje neprekidan tok. `Player._run` svakih 20 ms (apsolutno merenje vremena, bez drifta) šalje ili govor ili tišinu. To je glavni razlog što je zvuk u v2 adapteru čist.

## 2. Šta sve mora da radi (redosled pokretanja od nule)

Portovi: llama-server `8082`, whisper-server `8081`, AudioSocket adapter `9092`, SIP `5060`.

**Korak A – sistemski paketi (Ubuntu)**

```bash
sudo apt update
sudo apt install -y asterisk ffmpeg git python3-venv python3-pip build-essential cmake curl
```

Proveri da je modul učitan: `sudo asterisk -rx "module show like audiosocket"` (treba `app_audiosocket` i `res_audiosocket`). Ako ga nema, tvoj Asterisk paket nema AudioSocket i poziv na 601 neće raditi.

**Korak B – repo i venv** (u README: `~/local-voice/venv`)

```bash
git clone https://github.com/masterbg94/AIVA-Master.git ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
pip install edge-tts piper-tts          # edge-tts: opciono ali default; piper: lokalni TTS
pip install google-api-python-client google-auth   # SAMO ako koristiš Google Calendar
```

Adapter i agent koriste uglavnom standardnu biblioteku. Spoljni alati koje zovu: `ffmpeg` (obavezno, mora imati soxr za najbolji resampling; ako nema, kod sam pada na običan `aresample`), `piper` (ako `AIVA_TTS=piper`). `pw-play` i `pw-record` trebaju samo za lokalni mikrofon režim `sim_zubar*.py`, ne za adapter.

**Korak C – Piper model**
Kod očekuje `~/models/piper/sr_Marko_medium.onnx` (uz njega mora ići .onnx.json). Provera: `ls ~/models/piper/`.

**Korak D – llama.cpp (LLM, port 8082)** iz [readme2.md]

```bash
cd ~/llama.cpp && ./build/bin/llama-server \
  -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M \
  --device Vulkan0 -ngl 99 --host 127.0.0.1 --port 8082 -c 4096
```

NAPOMENA: u `readme.md` ista komanda je napisana u više redova BEZ `\` – ako je kopiraš tako, shell će je pokvariti. Koristi jednolinijsku verziju iz `readme2.md` ili ovu sa `\`. Provera: `curl -s http://127.0.0.1:8082/health`.

**Korak E – whisper.cpp (STT, port 8081)** iz [readme2.md]

```bash
cd ~/whisper.cpp/build/bin && ./whisper-server -m ../../models/ggml-large-v3-turbo-q5_0.bin \
  --host 127.0.0.1 --port 8081 -l sr -t 4
```

Provera: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/` (očekuj 200).

**Korak F – Asterisk**

```bash
sudo systemctl enable --now asterisk
sudo asterisk -rvvv                    # konzola
```

U konzoli: `pjsip show endpoints`, `pjsip show contacts` (Linphone mora biti registrovan), `dialplan show`.

**Korak G – Python adapter (test režim, bez pravih rezervacija)**

```bash
cd ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Treba da vidiš: `[SIP] Slušam na 127.0.0.1:9092; test režim, bez rezervacija; TTS=piper`. Za prave rezervacije dodaj `AIVA_LIVE_BOOKING=1`.

**Korak H – poziv**: u Linphone-u (registrovan na Asterisk) pozovi `601`.

VAŽNO o TTS-u [VIDLJIVO U KODU]: podrazumevano je `AIVA_TTS=edge`, što šalje tekst Microsoft servisu (potreban internet; nije lokalno). Za potpuno lokalni rad koristi `AIVA_TTS=piper`. Takođe, `main()` u adapteru na startu radi `warm_cache` bez try/except – ako TTS na startu ne uspe, adapter se ruši pre nego što počne da sluša.

Podešavanja okruženja (adapter): `AIVA_RMS_THRESHOLD` (250), `AIVA_END_SILENCE_MS` (900), `AIVA_MAX_TURNS` (20), `AIVA_TTS`, `AIVA_PIPER_CYR`, `AIVA_TARGET_RMS` (2600), `AIVA_PEAK` (26000), `AIVA_LIVE_BOOKING`, `AIVA_DUMP_DIR`, `AIVA_PIPER_EXTRA`. Agent: `WHISPER_URL`, `LLM_URL`, `LLM_PROVIDER` (local|nvidia), `GOOGLE_SA_FILE`, `GOOGLE_CALENDAR_ID`, `OWNER_PHONE_NUMBER`.

## 3. TODO 1 – Asterisk komande koje ti trebaju

```bash
sudo asterisk -rvvv                      # uđi u CLI (v = verbose)
sudo asterisk -rx "core show channels"   # aktivni pozivi (jedna komanda bez ulaska u CLI)
sudo asterisk -rx "pjsip show endpoints" # endpointi (601)
sudo asterisk -rx "pjsip show contacts"  # da li je Linphone registrovan i sa koje IP
sudo asterisk -rx "dialplan show"        # ceo dialplan
sudo asterisk -rx "dialplan reload"      # posle izmene extensions.conf
sudo asterisk -rx "module reload res_pjsip.so"  # posle izmene pjsip.conf (ili: core reload)
sudo asterisk -rx "module show like audiosocket"
sudo asterisk -rx "core set verbose 5"
sudo asterisk -rx "pjsip set logger on"  # SIP poruke u konzoli (za debug registracije)
sudo ss -ltnp | grep 9092                # da li adapter sluša
sudo journalctl -u asterisk -f           # sistemski log servisa
```

Šablon dialplan-a za 601 [ZAKLJUČAK iz koda adaptera + Asterisk dokumentacije; tvoj stvarni fajl proveri sa `sudo grep -n -B2 -A8 -i audiosocket /etc/asterisk/extensions.conf`]:

```
; context mora biti ISTI kao context= u endpointu 601 u pjsip.conf
[from-internal]
exten => 601,1,Answer()
 same => n,Set(CALLUUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,AudioSocket(${CALLUUID},127.0.0.1:9092)
 same => n,Hangup()
```

AudioSocket traži UUID u standardnom obliku 8-4-4-4-12; `${UNIQUEID}` NIJE validan. Adapter proverava samo da je prvi paket tipa `0x01` sa tačno 16 bajta, pa za jedan poziv radi i fiksni UUID, npr. `AudioSocket(11111111-2222-3333-4444-555555555555,127.0.0.1:9092)`. Ako `SHELL()` nije dostupan: `sudo asterisk -rx "core show function SHELL"`.

Šablon PJSIP endpointa (iz Asterisk dokumentacije; lozinke zameni):

```
[601]
type=endpoint
context=from-internal
disallow=all
allow=ulaw
allow=alaw
auth=auth601
aors=601

[auth601]
type=auth
auth_type=userpass
username=601
password=PROMENI_ME

[601]
type=aor
max_contacts=1
```

Codec `ulaw/alaw` je 8 kHz, što se poklapa sa 8 kHz tokom koji adapter očekuje.

## 4. TODO 3 – Kako pokrenuti `sim_zubar8_fixed.py` umesto `asterisk_adapter2.py`

Činjenice:

- `asterisk_adapter2.py` ima tvrdo upisano `import sim_zubar7 as agent`. Znači, adapter NE koristi `fixed`.
- Vidljivi deo `sim_zubar8_fixed.py` je isti tip fajla kao `sim_zubar7` (lokalni režim: `pw-record` mikrofon + `pw-play`, `input()` ENTER). Docstring i dalje kaže `sim_zubar7.py`. Jedina vidljiva razlika: `OWNER_PHONE_NUMBER` podrazumevano `""` (u `sim_zubar7/8` je upisan stvaran broj).
- `readme2.md` (V2) pokreće `fixed` sa `AIVA_TTS=piper AIVA_LIVE_BOOKING=0`. To su baš promenljive adaptera. Fajl `fixed` je 60 KB naspram 41 KB (razlika ≈ 19 KB ≈ veličina adaptera od 21 KB).
- [ZAKLJUČAK, NIJE POTVRĐENO]: najverovatnije je u `fixed` spojen adapter u isti fajl (jedan proces: AudioSocket server + agent). Nisam mogao da vidim rep fajla.

Proveri ovo PRE bilo čega (lokalno, u repou):

```bash
python3 -m py_compile sim_zubar8_fixed.py && echo SINTAKSA_OK
grep -n "9092\|start_server\|AIVA_\|__main__\|def next_reply\|def main" sim_zubar8_fixed.py
```

**Scenario A** – grep pokazuje `start_server`, `9092`, `AIVA_*`, `__main__`: fajl je samostalan. Pokretanje:

```bash
LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0 python3 -u ./sim_zubar8_fixed.py
```

Asterisk se ne dira ako i dalje sluša na `127.0.0.1:9092`. Ne pokreći ga istovremeno sa `asterisk_adapter2.py` (isti port → `Address already in use`).

**Scenario B** – nema `start_server`/`9092`: `fixed` je samo agent modul. Tada ga koristi adapter. Najčistija izmena u `asterisk_adapter2.py` (umesto `import sim_zubar7 as agent`):

```python
import importlib, os
agent = importlib.import_module(os.getenv("AIVA_AGENT", "sim_zubar7"))
```

pa:

```bash
AIVA_AGENT=sim_zubar8_fixed AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Uslov: modul mora da ima ono što adapter koristi: `WORK_DAYS`, `DAILY_SLOTS`, `is_slot_in_future`, `CalendarManager`, `extract` (adapter prihvata 2 ili 3 parametra preko `inspect`), `transcribe`, `normalize_phone`, `validate_name`, `next_reply(info, state, text, calendar)` koji vraća trojku `(reply, done, transfer)`, `_generate_edge_tts_wav`, `_generate_piper_wav`, `latin_to_cyrillic`, `PIPER_MODEL`.

## 5. TODO 4 – Log u terminalu (šta si rekao na telefon)

Adapter to VEĆ radi [VIDLJIVO U KODU], ali samo ako ga pokreneš u foreground terminalu sa `python3 -u`:

- `[SIP] Novi poziv` – Asterisk se povezao.
- `[VAD] Replika: 3.2 s` – detektovan govor.
- `[ASR] 'može sutra u deset i trideset'` – šta je Whisper razumeo (to je ono što tražiš).
- `[LLM] {...}` – izvučeni JSON.
- `[STATE] {...}` – stanje razgovora (ime, telefon, datum…).
- `[SIP] Poziv završen`.

Ako vidiš samo `Slušam na 127.0.0.1:9092` a ništa pri pozivu: poziv ne stiže do adaptera → problem je u dialplanu/modulu (`module show like audiosocket`) ili je adapter zauzet (`busy`).

Dodatni dijagnostički alati:

- `AIVA_DUMP_DIR=/tmp/aiva` snima PCM koji se šalje ka telefonu; preslušaj: `ffplay -f s16le -ar 8000 -ac 1 /tmp/aiva/<fajl>.raw`.
- Asterisk strana: `sudo asterisk -rvvv` + `core set verbose 5`.

Privatnost: `[ASR]` i `[STATE]` ispisuju ime i telefon pacijenta u čistom tekstu. Agent ima `mask_pii()` ali ga adapter ne koristi. Za test je OK; za produkciju stavi iza env prekidača (npr. `AIVA_LOG_ASR=1`).

## 6. TODO 5 – Da li menjati Asterisk config kad menjam Python skriptu?

Kratko: NE, dokle god nova skripta sluša na istoj adresi/portu i priča AudioSocket protokolom.

Moraš da menjaš `extensions.conf` samo ako: (1) menjaš port ili host (`127.0.0.1:9092`); (2) nova skripta ne koristi AudioSocket (npr. ARI/ExternalMedia ili lokalni mikrofon režim) – tada poziv sa 601 ne može da je dosegne; (3) dodaješ nove test brojeve (899, 600).

`pjsip.conf` ne diraš zbog promene Python programa – on se tiče samo SIP registracije (601, lozinka, codec, transport). Menjaš ga samo ako se menja uređaj/mreža/IP.

Posle svake izmene:

```bash
sudoedit /etc/asterisk/extensions.conf
sudo asterisk -rx "dialplan reload"
sudoedit /etc/asterisk/pjsip.conf
sudo asterisk -rx "module reload res_pjsip.so"
```

Savet: pre izmene napravi kopiju: `sudo cp /etc/asterisk/extensions.conf /etc/asterisk/extensions.conf.bak`.

## 7. TODO 6 – Test brojevi 899 i 600

**899 – eho test (mreža + codec + Linphone)**. Tvoj primer je ispravan. Predlog sa jasnijim tokom:

```
exten => 899,1,Answer()
 same => n,Wait(1)
 same => n,Playback(demo-echotest)
 same => n,Echo()
 same => n,Playback(demo-echodone)
 same => n,Hangup()
```

`demo-echotest` je u zvaničnom primeru `extensions.conf.sample`. Proveri da zvukovi postoje: `ls /var/lib/asterisk/sounds/en/ | grep demo-`. Ako ne, ostavi `demo-congrats` kao u tvom primeru.

Ograničenje: Echo meri samo put Linphone ↔ Asterisk. Ne testira kvalitet AI glasa. Ako je eho čist, a AI glas šušti, problem je u adapteru/TTS-u, ne u mreži.

**898 (predlog) – isti TTS zvuk bez AI-a**: napravi fajl u istom formatu koji adapter šalje i pusti ga iz Asteriska.

```bash
sudo mkdir -p /var/lib/asterisk/sounds/custom
ffmpeg -i neki_tts.wav -af "highpass=f=90,lowpass=f=3500" -ar 8000 -ac 1 -c:a pcm_s16le aiva-test.wav
sudo mv aiva-test.wav /var/lib/asterisk/sounds/custom/ && sudo chown asterisk:asterisk /var/lib/asterisk/sounds/custom/aiva-test.wav
```

```
exten => 898,1,Answer()
 same => n,Playback(custom/aiva-test)
 same => n,Hangup()
```

Tumačenje: 898 čist + AI poziv šušti → greška u slanju okvira (CPU zastoji, `Player` sat). 898 takođe šušti → codec/mreža/Linphone.

**600 – provera da je sve pokrenuto**. Dialplan sam ne može da proveri Whisper/LLM, pa pozove skriptu. Napravi `/usr/local/bin/aiva-health.sh`:

```bash
#!/bin/bash
ok=0
curl -s -m 2 -o /dev/null http://127.0.0.1:8081/ && ok=$((ok+1))
curl -s -m 2 http://127.0.0.1:8082/health | grep -q ok && ok=$((ok+1))
ss -ltn | grep -q ':9092 ' && ok=$((ok+1))
command -v ffmpeg >/dev/null && ok=$((ok+1))
echo -n $ok
```

```bash
sudo chmod +x /usr/local/bin/aiva-health.sh
```

```
exten => 600,1,Answer()
 same => n,Set(N=${SHELL(/usr/local/bin/aiva-health.sh)})
 same => n,GotoIf($["${N}" = "4"]?ok:bad)
 same => n(ok),Playback(beep)
 same => n,SayNumber(${N})
 same => n,Hangup()
 same => n(bad),SayNumber(${N})
 same => n,Playback(beep)
 same => n,Hangup()
```

Značenje: izgovorena cifra 4 = svih 4 provere prošle (Whisper, LLM, adapter na 9092, ffmpeg). Isti skript možeš pokrenuti i u terminalu (`/usr/local/bin/aiva-health.sh`) – to je pouzdanija prva provera. Ovaj 600 dialplan nije testiran na tvom sistemu; ako `SHELL()` nije učitan, koristi samo terminalnu verziju.

Ne zaboravi `sudo asterisk -rx "dialplan reload"` i da brojevi budu u istom contextu kao 601.

## 8. Code review – nalazi po prioritetu

**P0 – proveri odmah**

- [VIDLJIVO U KODU] U `sim_zubar7.py`, `sim_zubar8.py` i `sim_zubar8_fixed.py` postoje zakomentarisani redovi `# def http_json(...)` i `# def llm(...)` čije telo NIJE zakomentarisano (uvučene linije). Po mom čitanju Python bi to odbio sa `IndentationError: unexpected indent`, pa `import sim_zubar7` u adapteru ne bi prošao. Ako lokalno radi, onda se lokalna kopija razlikuje od GitHub-a. Provera: `python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py`. Popravka: obriši sve zakomentarisane stare verzije (ostaje po jedna `def http_json` i `def llm`).
- `sim_zubar7.py` i `sim_zubar8.py` imaju identičan SHA (`ab4f72e8…`, 41081 B) – dupli fajl. Obriši jedan.
- Ličnost/privatnost: u `sim_zubar7/8` je javni repo sa upisanim `OWNER_PHONE_NUMBER="+381640877180"`. Premesti u env i obriši iz istorije ako ne želiš da je javno. `osmeh_bookings.db` i `__pycache__/` su commitovani – dodaj u `.gitignore` (baza može sadržati podatke pacijenata).

**P1 – logika**

- `CalendarManager.book_slot`: upis u SQLite se commit-uje PRE provere/upisa u Google Calendar. Ako je slot zauzet u Google-u ili `events().insert` baci grešku, u SQLite ostaje fantomski termin koji blokira slot. Rešenje: upis u Google prvo, pa SQLite; ili `DELETE` u `except`/pri `return False`.
- `sqlite3.connect()` kao context manager radi commit/rollback, ali NE zatvara konekciju – koristi `contextlib.closing`.
- `validate_name` zahteva razmak (ime + prezime); jedno ime se odbija.
- `parse_serbian_time_fallback` pokriva samo vaših 5 slotova; `"devet"` u „devet i trideset” vraća 09:00.
- Adapter: `warm_cache` bez try/except; jedan poziv istovremeno (`busy`); nema barge-in (korisnik ne može da prekine asistenta); docstring adaptera kaže `asterisk_adapter.py` i `sim_zubar7` – zastareo.
- `LLM_PROVIDER=nvidia` šalje transkript pacijenta u oblak; naziv modela `nvidia/nemotron-3.5-bolt-e4b` nisam mogao da potvrdim – proveri u NVIDIA katalogu.

**P2 – higijena**

- 8 verzija `sim_zubar*.py` u korenu: premesti stare u `archive/` ili koristi git tagove.
- Dodaj `requirements.txt` i `run.sh` sa env promenljivama.
- Dodaj `pytest` testove za čiste funkcije (`parse_serbian_time_fallback`, `normalize_phone`, `spoken_phone`, `cyr_to_lat`, `split_sentences`, `next_reply` sa lažnim kalendarom).
- Pokretanje kao `systemd` servisi (llama, whisper, adapter) kada test prođe.

## 9. Šta ostaje da potvrdiš (da bi bilo 100%)

Pošalji izlaz ovih komandi i dovršiću analizu bez nagađanja:

```bash
cd ~/Documents/Workspace/test
python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py asterisk_adapter2.py; echo exit=$?
wc -c sim_zubar7.py sim_zubar8_fixed.py
grep -n "def next_reply\|start_server\|9092\|AIVA_\|__main__" sim_zubar8_fixed.py
sudo grep -n -B2 -A8 -i "audiosocket\|601" /etc/asterisk/extensions.conf
sudo grep -n -A10 "\[601\]" /etc/asterisk/pjsip.conf
sudo asterisk -rx "module show like audiosocket"
```

Brzina odziva (Whisper turbo + Qwen3-4B na Vulkan + TTS po replici) nije merena; ne mogu da tvrdim latenciju bez tvog logova.## 0. Šta je pregledano, a šta NIJE (iskreno)

Pročitano u celosti: struktura repoa, `readme.md`, `readme2.md`, `todo.md`, `asterisk_adapter2.py`.

Pročitano delimično: `sim_zubar7.py`, `sim_zubar8_fixed.py`. GitHub konektor je odsekao sadržaj posle `extract()` prompta (otprilike prvih 2/3 `sim_zubar7`, još manje `fixed`). Nisam video: `next_reply()`, dijaloški state machine, glavnu petlju i sve što je u `fixed` iznad 41 KB.

Nije čitano: `sim_test.py`, `sim_zubar.py`–`sim_zubar5.py`, `my_voice_test.py`, `omniVoiceTest/`, `osmeh_bookings.db` (binarno), `.vscode/`.

Posebno: Asterisk konfiguracija (`/etc/asterisk/*.conf`) NIJE u repou. Zato su dole primeri šabloni, a ne tvoj stvarni config. Svaka tvrdnja je označena kao [VIDLJIVO U KODU], [ZAKLJUČAK] ili [PROVERI KOMANDOM].

## 1. Arhitektura – kako sve radi

```
Linphone (601)
   │ SIP/RTP (ulaw/alaw, 8 kHz)
   ▼
Asterisk (PJSIP + dialplan)
   │ AudioSocket(uuid,127.0.0.1:9092)  ← TCP, slin 8 kHz, 16-bit, mono
   ▼
asterisk_adapter2.py  (Python asyncio server)
   ├─ VAD (RMS prag) → ceo iskaz pacijenta
   ├─ ffmpeg → 16 kHz WAV → Whisper server  :8081 (/inference)  = STT
   ├─ sim_zubar7.extract() → llama-server   :8082 (/v1/chat/completions) = JSON intent
   ├─ sim_zubar7.next_reply() = deterministički state machine zakazivanja
   ├─ TTS: edge-tts (INTERNET) ili Piper (lokalno)
   └─ ffmpeg filteri → soxr 8 kHz → normalizacija → 20 ms okviri → nazad u Asterisk
```

Ključna ideja [VIDLJIVO U KODU]: LLM ne vodi razgovor slobodno. On samo izvlači JSON (`intent`, `date_iso`, `time`, `name`, `phone`, `confirm`), a odluku šta se odgovara donosi Python kod (`next_reply`). Zato je ponašanje predvidljivo. Dodatni slojevi sigurnosti: `sanitize_user_text` (300 znakova, bez kontrolnih znakova), whitelist `ALLOWED_INTENTS`, `validate_name/phone/time`, deterministički parser vremena `parse_serbian_time_fallback`, zabrana termina u prošlosti (`MIN_LEAD_MINUTES=30`).

AudioSocket protokol [VIDLJIVO U KODU + Asterisk dokumentacija]: svaki paket = 1 bajt tip + 2 bajta dužina (big-endian) + payload. `0x01` = 16 bajta UUID poziva (prvi paket), `0x10` = audio, `0x00` = prekid/hangup, `0xFF` = greška. Okvir je 320 bajta = 20 ms @ 8 kHz, 16-bit mono.

Zašto `Player` šalje tišinu: Asterisk očekuje neprekidan tok. `Player._run` svakih 20 ms (apsolutno merenje vremena, bez drifta) šalje ili govor ili tišinu. To je glavni razlog što je zvuk u v2 adapteru čist.

## 2. Šta sve mora da radi (redosled pokretanja od nule)

Portovi: llama-server `8082`, whisper-server `8081`, AudioSocket adapter `9092`, SIP `5060`.

**Korak A – sistemski paketi (Ubuntu)**

```bash
sudo apt update
sudo apt install -y asterisk ffmpeg git python3-venv python3-pip build-essential cmake curl
```

Proveri da je modul učitan: `sudo asterisk -rx "module show like audiosocket"` (treba `app_audiosocket` i `res_audiosocket`). Ako ga nema, tvoj Asterisk paket nema AudioSocket i poziv na 601 neće raditi.

**Korak B – repo i venv** (u README: `~/local-voice/venv`)

```bash
git clone https://github.com/masterbg94/AIVA-Master.git ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
pip install edge-tts piper-tts          # edge-tts: opciono ali default; piper: lokalni TTS
pip install google-api-python-client google-auth   # SAMO ako koristiš Google Calendar
```

Adapter i agent koriste uglavnom standardnu biblioteku. Spoljni alati koje zovu: `ffmpeg` (obavezno, mora imati soxr za najbolji resampling; ako nema, kod sam pada na običan `aresample`), `piper` (ako `AIVA_TTS=piper`). `pw-play` i `pw-record` trebaju samo za lokalni mikrofon režim `sim_zubar*.py`, ne za adapter.

**Korak C – Piper model**
Kod očekuje `~/models/piper/sr_Marko_medium.onnx` (uz njega mora ići .onnx.json). Provera: `ls ~/models/piper/`.

**Korak D – llama.cpp (LLM, port 8082)** iz [readme2.md]

```bash
cd ~/llama.cpp && ./build/bin/llama-server \
  -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M \
  --device Vulkan0 -ngl 99 --host 127.0.0.1 --port 8082 -c 4096
```

NAPOMENA: u `readme.md` ista komanda je napisana u više redova BEZ `\` – ako je kopiraš tako, shell će je pokvariti. Koristi jednolinijsku verziju iz `readme2.md` ili ovu sa `\`. Provera: `curl -s http://127.0.0.1:8082/health`.

**Korak E – whisper.cpp (STT, port 8081)** iz [readme2.md]

```bash
cd ~/whisper.cpp/build/bin && ./whisper-server -m ../../models/ggml-large-v3-turbo-q5_0.bin \
  --host 127.0.0.1 --port 8081 -l sr -t 4
```

Provera: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8081/` (očekuj 200).

**Korak F – Asterisk**

```bash
sudo systemctl enable --now asterisk
sudo asterisk -rvvv                    # konzola
```

U konzoli: `pjsip show endpoints`, `pjsip show contacts` (Linphone mora biti registrovan), `dialplan show`.

**Korak G – Python adapter (test režim, bez pravih rezervacija)**

```bash
cd ~/Documents/Workspace/test
source ~/local-voice/venv/bin/activate
AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Treba da vidiš: `[SIP] Slušam na 127.0.0.1:9092; test režim, bez rezervacija; TTS=piper`. Za prave rezervacije dodaj `AIVA_LIVE_BOOKING=1`.

**Korak H – poziv**: u Linphone-u (registrovan na Asterisk) pozovi `601`.

VAŽNO o TTS-u [VIDLJIVO U KODU]: podrazumevano je `AIVA_TTS=edge`, što šalje tekst Microsoft servisu (potreban internet; nije lokalno). Za potpuno lokalni rad koristi `AIVA_TTS=piper`. Takođe, `main()` u adapteru na startu radi `warm_cache` bez try/except – ako TTS na startu ne uspe, adapter se ruši pre nego što počne da sluša.

Podešavanja okruženja (adapter): `AIVA_RMS_THRESHOLD` (250), `AIVA_END_SILENCE_MS` (900), `AIVA_MAX_TURNS` (20), `AIVA_TTS`, `AIVA_PIPER_CYR`, `AIVA_TARGET_RMS` (2600), `AIVA_PEAK` (26000), `AIVA_LIVE_BOOKING`, `AIVA_DUMP_DIR`, `AIVA_PIPER_EXTRA`. Agent: `WHISPER_URL`, `LLM_URL`, `LLM_PROVIDER` (local|nvidia), `GOOGLE_SA_FILE`, `GOOGLE_CALENDAR_ID`, `OWNER_PHONE_NUMBER`.

## 3. TODO 1 – Asterisk komande koje ti trebaju

```bash
sudo asterisk -rvvv                      # uđi u CLI (v = verbose)
sudo asterisk -rx "core show channels"   # aktivni pozivi (jedna komanda bez ulaska u CLI)
sudo asterisk -rx "pjsip show endpoints" # endpointi (601)
sudo asterisk -rx "pjsip show contacts"  # da li je Linphone registrovan i sa koje IP
sudo asterisk -rx "dialplan show"        # ceo dialplan
sudo asterisk -rx "dialplan reload"      # posle izmene extensions.conf
sudo asterisk -rx "module reload res_pjsip.so"  # posle izmene pjsip.conf (ili: core reload)
sudo asterisk -rx "module show like audiosocket"
sudo asterisk -rx "core set verbose 5"
sudo asterisk -rx "pjsip set logger on"  # SIP poruke u konzoli (za debug registracije)
sudo ss -ltnp | grep 9092                # da li adapter sluša
sudo journalctl -u asterisk -f           # sistemski log servisa
```

Šablon dialplan-a za 601 [ZAKLJUČAK iz koda adaptera + Asterisk dokumentacije; tvoj stvarni fajl proveri sa `sudo grep -n -B2 -A8 -i audiosocket /etc/asterisk/extensions.conf`]:

```
; context mora biti ISTI kao context= u endpointu 601 u pjsip.conf
[from-internal]
exten => 601,1,Answer()
 same => n,Set(CALLUUID=${SHELL(uuidgen | tr -d '\n')})
 same => n,AudioSocket(${CALLUUID},127.0.0.1:9092)
 same => n,Hangup()
```

AudioSocket traži UUID u standardnom obliku 8-4-4-4-12; `${UNIQUEID}` NIJE validan. Adapter proverava samo da je prvi paket tipa `0x01` sa tačno 16 bajta, pa za jedan poziv radi i fiksni UUID, npr. `AudioSocket(11111111-2222-3333-4444-555555555555,127.0.0.1:9092)`. Ako `SHELL()` nije dostupan: `sudo asterisk -rx "core show function SHELL"`.

Šablon PJSIP endpointa (iz Asterisk dokumentacije; lozinke zameni):

```
[601]
type=endpoint
context=from-internal
disallow=all
allow=ulaw
allow=alaw
auth=auth601
aors=601

[auth601]
type=auth
auth_type=userpass
username=601
password=PROMENI_ME

[601]
type=aor
max_contacts=1
```

Codec `ulaw/alaw` je 8 kHz, što se poklapa sa 8 kHz tokom koji adapter očekuje.

## 4. TODO 3 – Kako pokrenuti `sim_zubar8_fixed.py` umesto `asterisk_adapter2.py`

Činjenice:

- `asterisk_adapter2.py` ima tvrdo upisano `import sim_zubar7 as agent`. Znači, adapter NE koristi `fixed`.
- Vidljivi deo `sim_zubar8_fixed.py` je isti tip fajla kao `sim_zubar7` (lokalni režim: `pw-record` mikrofon + `pw-play`, `input()` ENTER). Docstring i dalje kaže `sim_zubar7.py`. Jedina vidljiva razlika: `OWNER_PHONE_NUMBER` podrazumevano `""` (u `sim_zubar7/8` je upisan stvaran broj).
- `readme2.md` (V2) pokreće `fixed` sa `AIVA_TTS=piper AIVA_LIVE_BOOKING=0`. To su baš promenljive adaptera. Fajl `fixed` je 60 KB naspram 41 KB (razlika ≈ 19 KB ≈ veličina adaptera od 21 KB).
- [ZAKLJUČAK, NIJE POTVRĐENO]: najverovatnije je u `fixed` spojen adapter u isti fajl (jedan proces: AudioSocket server + agent). Nisam mogao da vidim rep fajla.

Proveri ovo PRE bilo čega (lokalno, u repou):

```bash
python3 -m py_compile sim_zubar8_fixed.py && echo SINTAKSA_OK
grep -n "9092\|start_server\|AIVA_\|__main__\|def next_reply\|def main" sim_zubar8_fixed.py
```

**Scenario A** – grep pokazuje `start_server`, `9092`, `AIVA_*`, `__main__`: fajl je samostalan. Pokretanje:

```bash
LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0 python3 -u ./sim_zubar8_fixed.py
```

Asterisk se ne dira ako i dalje sluša na `127.0.0.1:9092`. Ne pokreći ga istovremeno sa `asterisk_adapter2.py` (isti port → `Address already in use`).

**Scenario B** – nema `start_server`/`9092`: `fixed` je samo agent modul. Tada ga koristi adapter. Najčistija izmena u `asterisk_adapter2.py` (umesto `import sim_zubar7 as agent`):

```python
import importlib, os
agent = importlib.import_module(os.getenv("AIVA_AGENT", "sim_zubar7"))
```

pa:

```bash
AIVA_AGENT=sim_zubar8_fixed AIVA_TTS=piper python3 -u asterisk_adapter2.py
```

Uslov: modul mora da ima ono što adapter koristi: `WORK_DAYS`, `DAILY_SLOTS`, `is_slot_in_future`, `CalendarManager`, `extract` (adapter prihvata 2 ili 3 parametra preko `inspect`), `transcribe`, `normalize_phone`, `validate_name`, `next_reply(info, state, text, calendar)` koji vraća trojku `(reply, done, transfer)`, `_generate_edge_tts_wav`, `_generate_piper_wav`, `latin_to_cyrillic`, `PIPER_MODEL`.

## 5. TODO 4 – Log u terminalu (šta si rekao na telefon)

Adapter to VEĆ radi [VIDLJIVO U KODU], ali samo ako ga pokreneš u foreground terminalu sa `python3 -u`:

- `[SIP] Novi poziv` – Asterisk se povezao.
- `[VAD] Replika: 3.2 s` – detektovan govor.
- `[ASR] 'može sutra u deset i trideset'` – šta je Whisper razumeo (to je ono što tražiš).
- `[LLM] {...}` – izvučeni JSON.
- `[STATE] {...}` – stanje razgovora (ime, telefon, datum…).
- `[SIP] Poziv završen`.

Ako vidiš samo `Slušam na 127.0.0.1:9092` a ništa pri pozivu: poziv ne stiže do adaptera → problem je u dialplanu/modulu (`module show like audiosocket`) ili je adapter zauzet (`busy`).

Dodatni dijagnostički alati:

- `AIVA_DUMP_DIR=/tmp/aiva` snima PCM koji se šalje ka telefonu; preslušaj: `ffplay -f s16le -ar 8000 -ac 1 /tmp/aiva/<fajl>.raw`.
- Asterisk strana: `sudo asterisk -rvvv` + `core set verbose 5`.

Privatnost: `[ASR]` i `[STATE]` ispisuju ime i telefon pacijenta u čistom tekstu. Agent ima `mask_pii()` ali ga adapter ne koristi. Za test je OK; za produkciju stavi iza env prekidača (npr. `AIVA_LOG_ASR=1`).

## 6. TODO 5 – Da li menjati Asterisk config kad menjam Python skriptu?

Kratko: NE, dokle god nova skripta sluša na istoj adresi/portu i priča AudioSocket protokolom.

Moraš da menjaš `extensions.conf` samo ako: (1) menjaš port ili host (`127.0.0.1:9092`); (2) nova skripta ne koristi AudioSocket (npr. ARI/ExternalMedia ili lokalni mikrofon režim) – tada poziv sa 601 ne može da je dosegne; (3) dodaješ nove test brojeve (899, 600).

`pjsip.conf` ne diraš zbog promene Python programa – on se tiče samo SIP registracije (601, lozinka, codec, transport). Menjaš ga samo ako se menja uređaj/mreža/IP.

Posle svake izmene:

```bash
sudoedit /etc/asterisk/extensions.conf
sudo asterisk -rx "dialplan reload"
sudoedit /etc/asterisk/pjsip.conf
sudo asterisk -rx "module reload res_pjsip.so"
```

Savet: pre izmene napravi kopiju: `sudo cp /etc/asterisk/extensions.conf /etc/asterisk/extensions.conf.bak`.

## 7. TODO 6 – Test brojevi 899 i 600

**899 – eho test (mreža + codec + Linphone)**. Tvoj primer je ispravan. Predlog sa jasnijim tokom:

```
exten => 899,1,Answer()
 same => n,Wait(1)
 same => n,Playback(demo-echotest)
 same => n,Echo()
 same => n,Playback(demo-echodone)
 same => n,Hangup()
```

`demo-echotest` je u zvaničnom primeru `extensions.conf.sample`. Proveri da zvukovi postoje: `ls /var/lib/asterisk/sounds/en/ | grep demo-`. Ako ne, ostavi `demo-congrats` kao u tvom primeru.

Ograničenje: Echo meri samo put Linphone ↔ Asterisk. Ne testira kvalitet AI glasa. Ako je eho čist, a AI glas šušti, problem je u adapteru/TTS-u, ne u mreži.

**898 (predlog) – isti TTS zvuk bez AI-a**: napravi fajl u istom formatu koji adapter šalje i pusti ga iz Asteriska.

```bash
sudo mkdir -p /var/lib/asterisk/sounds/custom
ffmpeg -i neki_tts.wav -af "highpass=f=90,lowpass=f=3500" -ar 8000 -ac 1 -c:a pcm_s16le aiva-test.wav
sudo mv aiva-test.wav /var/lib/asterisk/sounds/custom/ && sudo chown asterisk:asterisk /var/lib/asterisk/sounds/custom/aiva-test.wav
```

```
exten => 898,1,Answer()
 same => n,Playback(custom/aiva-test)
 same => n,Hangup()
```

Tumačenje: 898 čist + AI poziv šušti → greška u slanju okvira (CPU zastoji, `Player` sat). 898 takođe šušti → codec/mreža/Linphone.

**600 – provera da je sve pokrenuto**. Dialplan sam ne može da proveri Whisper/LLM, pa pozove skriptu. Napravi `/usr/local/bin/aiva-health.sh`:

```bash
#!/bin/bash
ok=0
curl -s -m 2 -o /dev/null http://127.0.0.1:8081/ && ok=$((ok+1))
curl -s -m 2 http://127.0.0.1:8082/health | grep -q ok && ok=$((ok+1))
ss -ltn | grep -q ':9092 ' && ok=$((ok+1))
command -v ffmpeg >/dev/null && ok=$((ok+1))
echo -n $ok
```

```bash
sudo chmod +x /usr/local/bin/aiva-health.sh
```

```
exten => 600,1,Answer()
 same => n,Set(N=${SHELL(/usr/local/bin/aiva-health.sh)})
 same => n,GotoIf($["${N}" = "4"]?ok:bad)
 same => n(ok),Playback(beep)
 same => n,SayNumber(${N})
 same => n,Hangup()
 same => n(bad),SayNumber(${N})
 same => n,Playback(beep)
 same => n,Hangup()
```

Značenje: izgovorena cifra 4 = svih 4 provere prošle (Whisper, LLM, adapter na 9092, ffmpeg). Isti skript možeš pokrenuti i u terminalu (`/usr/local/bin/aiva-health.sh`) – to je pouzdanija prva provera. Ovaj 600 dialplan nije testiran na tvom sistemu; ako `SHELL()` nije učitan, koristi samo terminalnu verziju.

Ne zaboravi `sudo asterisk -rx "dialplan reload"` i da brojevi budu u istom contextu kao 601.

## 8. Code review – nalazi po prioritetu

**P0 – proveri odmah**

- [VIDLJIVO U KODU] U `sim_zubar7.py`, `sim_zubar8.py` i `sim_zubar8_fixed.py` postoje zakomentarisani redovi `# def http_json(...)` i `# def llm(...)` čije telo NIJE zakomentarisano (uvučene linije). Po mom čitanju Python bi to odbio sa `IndentationError: unexpected indent`, pa `import sim_zubar7` u adapteru ne bi prošao. Ako lokalno radi, onda se lokalna kopija razlikuje od GitHub-a. Provera: `python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py`. Popravka: obriši sve zakomentarisane stare verzije (ostaje po jedna `def http_json` i `def llm`).
- `sim_zubar7.py` i `sim_zubar8.py` imaju identičan SHA (`ab4f72e8…`, 41081 B) – dupli fajl. Obriši jedan.
- Ličnost/privatnost: u `sim_zubar7/8` je javni repo sa upisanim `OWNER_PHONE_NUMBER="+381640877180"`. Premesti u env i obriši iz istorije ako ne želiš da je javno. `osmeh_bookings.db` i `__pycache__/` su commitovani – dodaj u `.gitignore` (baza može sadržati podatke pacijenata).

**P1 – logika**

- `CalendarManager.book_slot`: upis u SQLite se commit-uje PRE provere/upisa u Google Calendar. Ako je slot zauzet u Google-u ili `events().insert` baci grešku, u SQLite ostaje fantomski termin koji blokira slot. Rešenje: upis u Google prvo, pa SQLite; ili `DELETE` u `except`/pri `return False`.
- `sqlite3.connect()` kao context manager radi commit/rollback, ali NE zatvara konekciju – koristi `contextlib.closing`.
- `validate_name` zahteva razmak (ime + prezime); jedno ime se odbija.
- `parse_serbian_time_fallback` pokriva samo vaših 5 slotova; `"devet"` u „devet i trideset” vraća 09:00.
- Adapter: `warm_cache` bez try/except; jedan poziv istovremeno (`busy`); nema barge-in (korisnik ne može da prekine asistenta); docstring adaptera kaže `asterisk_adapter.py` i `sim_zubar7` – zastareo.
- `LLM_PROVIDER=nvidia` šalje transkript pacijenta u oblak; naziv modela `nvidia/nemotron-3.5-bolt-e4b` nisam mogao da potvrdim – proveri u NVIDIA katalogu.

**P2 – higijena**

- 8 verzija `sim_zubar*.py` u korenu: premesti stare u `archive/` ili koristi git tagove.
- Dodaj `requirements.txt` i `run.sh` sa env promenljivama.
- Dodaj `pytest` testove za čiste funkcije (`parse_serbian_time_fallback`, `normalize_phone`, `spoken_phone`, `cyr_to_lat`, `split_sentences`, `next_reply` sa lažnim kalendarom).
- Pokretanje kao `systemd` servisi (llama, whisper, adapter) kada test prođe.

## 9. Šta ostaje da potvrdiš (da bi bilo 100%)

Pošalji izlaz ovih komandi i dovršiću analizu bez nagađanja:

```bash
cd ~/Documents/Workspace/test
python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py asterisk_adapter2.py; echo exit=$?
wc -c sim_zubar7.py sim_zubar8_fixed.py
grep -n "def next_reply\|start_server\|9092\|AIVA_\|__main__" sim_zubar8_fixed.py
sudo grep -n -B2 -A8 -i "audiosocket\|601" /etc/asterisk/extensions.conf
sudo grep -n -A10 "\[601\]" /etc/asterisk/pjsip.conf
sudo asterisk -rx "module show like audiosocket"
```

Brzina odziva (Whisper turbo + Qwen3-4B na Vulkan + TTS po replici) nije merena; ne mogu da tvrdim latenciju bez tvog logova.
Pošalji izlaz ovih komandi i dovršiću analizu bez nagađanja:

```bash
cd ~/Documents/Workspace/test
python3 -m py_compile sim_zubar7.py sim_zubar8_fixed.py asterisk_adapter2.py; echo exit=$?
wc -c sim_zubar7.py sim_zubar8_fixed.py
grep -n "def next_reply\|start_server\|9092\|AIVA_\|__main__" sim_zubar8_fixed.py
sudo grep -n -B2 -A8 -i "audiosocket\|601" /etc/asterisk/extensions.conf
sudo grep -n -A10 "\[601\]" /etc/asterisk/pjsip.conf
sudo asterisk -rx "module show like audiosocket"
```

Brzina odziva (Whisper turbo + Qwen3-4B na Vulkan + TTS po replici) nije merena; ne mogu da tvrdim latenciju bez tvog logova.