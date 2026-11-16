#!/bin/sh
# Double-click to open Bill: starts the server if needed, then opens it in its own Chrome window.
DIR="$(cd "$(dirname "$0")" && pwd)"
URL="http://localhost:5070"
if ! curl -sf -o /dev/null "$URL/"; then
  cd "$DIR"
  nohup /usr/bin/python3 "$DIR/app.py" >> "$DIR/bill.log" 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    curl -sf -o /dev/null "$URL/" && break
    sleep 0.5
  done
fi
open -na "Google Chrome" --args --app="$URL"
