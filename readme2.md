# Project info about setup

## Setup
-  Venv:
```bash
(venv) master@master-iMac19-1:~/Documents/Workspace/test$ which python3
/home/master/local-voice/venv/bin/python3
(venv) master@master-iMac19-1:~/Documents/Workspace/test$ 
```

-  Local AI llm

`master@master-iMac19-1:~/llama.cpp$ `
```bash
cd ~/llama.cpp && ./build/bin/llama-server -hf unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M --device Vulkan0 -ngl 99 --host 127.0.0.1 --port 8082 -c 4096
```

-  Whisper
`master@master-iMac19-1:~/whisper.cpp/build/bin$`
```bash
cd ~/whisper.cpp/build/bin && ./whisper-server -m ../../models/ggml-large-v3-turbo-q5_0.bin --host 127.0.0.1 --port 8081 -l sr -t 4
```


## Pokretanje LINPHONE / Asterisk - cela app:

### **V1 Ok Sound** 

*Inicijalno dobra verzija , lep zvuk sredjeno iz `asterisk_adapter.py` verzije i napravljen `asterisk_adapter2.py`*

```bash
python3 -u asterisk_adapter2.py
```


### **V2** 

*`sim_zubar8_fixed.py` update da se pokrece ne znam ni ja sta*

```bash
LLM_PROVIDER=local AIVA_TTS=piper AIVA_LIVE_BOOKING=0   python3 -u ./sim_zubar8_fixed.py
```


# Todo:
Ovde vidi i sta treba da se objasni i pregleda [TODO](./todo)