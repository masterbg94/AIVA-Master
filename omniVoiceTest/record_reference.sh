#!/usr/bin/env bash
# record_reference.sh - snima referentni glas za omni-custom. Samo svoj glas ili uz saglasnost osobe.
set -euo pipefail
DIR="$HOME/local-voice/voices/custom"
SECS="${1:-8}"
mkdir -p "$DIR"
TEXT="Dobar dan, dobili ste stomatološku ordinaciju. Recite mi koji termin vam odgovara."
echo "Pročitaj prirodnim tempom, jasno, bez buke (oko ${SECS}s):"
echo; echo "  $TEXT"; echo
read -r -p "ENTER = počni snimanje... "
ffmpeg -y -loglevel error -f pulse -i default -t "$SECS" -ac 1 -ar 24000 -c:a pcm_s16le "$DIR/reference.wav"
printf '%s\n' "$TEXT" > "$DIR/reference.txt"
echo "Snimljeno: $DIR/reference.wav"
echo "Prepis:    $DIR/reference.txt (ako si rekao nešto drugo, ispravi ga tačno)"
aplay -q "$DIR/reference.wav"