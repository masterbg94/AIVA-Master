## Todo - objasniti :
1. obasniti kako sve ovo radi , objasniti sve potrebne `Asterisk` komande. 
2. Objasni sve sto treba da se pokrene da bi ova skripta radila? Na primer `Asterisk` , `Whisper` , lokalni `llama.cpp` model , `piper` , neki `.venv` ako treba i slicno.
3. kako da zamenim glavnu skriptu na primer necu `python3 -u asterisk_adapter2.py` , nego hocu da pokrenem `sim_zubar8_fixed.py`?
4. za test treba napraviti da se prikazuje log u terminalu kada pokrenem skriptu, na primer da se prikazuje sta sam ja rekao na telefon kada sam pozvao preko LinPhone 601 ?
5. Da li treba da menjam nesto u konfiguraciji ili `Asterisk` config skripti kada menjam glavni pokretac python program koji vodi AI agent ? na primer `sudoedit /etc/asterisk/extensions.conf` ili `sudoedit /etc/asterisk/pjsip.conf` , jer se secam da smo tu nesto dodavali i menjali. 
6. Da li mozemo i kako da dodamo neki test na druge "portove" na primer na `Asterisk` setup da dodamo `899` da bude neki test kako radi AI i da li šušti , krči ili zvuk ima šum. Imas primer ispod sta sam mislio. Takodje da li mozemo da dodamo nekako i '600' da bude neki drugi test , na primer da li je sve spremljeno i pokrenuto kako treba ?
```
exten => 899,1,Answer()
 same => n,Playback(demo-congrats)
 same => n,Echo()
 same => n,Hangup()
``` 

**Sve iznad treba objasniti precizno i detaljno u jednom novom notion page**