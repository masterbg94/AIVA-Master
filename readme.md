# Testing guide:

## Kako videti sve venv:
```bash
find ~ -name "pyvenv.cfg" 2>/dev/null
```

## Šta se ranije koristilo

U ranijim porukama za postojeću aplikaciju sa Piper-om koristili smo:

```bash
source ~/local-voice/venv/bin/activate
```


Za OmniVoice smo odvojeno navodili:

```bash
source ~/local-voice/omnivoice-venv/bin/activate
```

A u tvom poslednjem terminalu piše:


`(.venv) master@master-iMac19-1:...`

To sugeriše drugo okruženje, ali sam natpis (.venv) ne otkriva njegovu stvarnu putanju.



## Fajlovi:
- sim_test.py - lose STT
- sim_zubar.py - lose opet
- sim_zubar2.py -> prihvatljivo
- sim_zubar3.py -> prihvatljivo -> testirati jos


## pokretanje agenta:

### llama cpp main agent :

```bash
master@master-iMac19-1:~/llama.cpp$ pwd
/home/master/llama.cpp
```
- pokretanje
```bash
./build/bin/llama-server
-hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
--device Vulkan0
-ngl 99
--host 127.0.0.1
--port 8082
-c 4096
```

### whisper agent:

```bash
(venv) master@master-iMac19-1:~/local-voice/venv$ find ~ -name "whisper-server" -type f 2>/dev/null
/home/master/whisper.cpp/build/bin/whisper-server
```
- pokretanje
```bash
./build/bin/whisper-server \
  -m models/ggml-large-v3-turbo-q5_0.bin \
  --host 127.0.0.1 \
  --port 8081 \
  -l sr \
  -t 4
```


 