
#!/usr/bin/env python3
import json
import time
import requests

URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:9b"

payload = {
    "model": MODEL,
    "messages": [
        {
            "role": "user",
            "content": (
                "Analiziraj kratko stanje SWGOH: "
                "main_menu, confidence 0.94. "
                "Vrati JSON sa summary i safe_next_step."
            ),
        }
    ],
    "stream": True,
    "think": True,
    "format": "json",
    "options": {
        "temperature": 0,
        "num_predict": 120,
    },
}

print(f"Model: {MODEL}")
print("Čekam odgovor; delovi će se prikazivati uživo...\n")

start = time.monotonic()
last_output = start

try:
    with requests.post(
        URL,
        json=payload,
        stream=True,
        timeout=(10, 300),
    ) as response:
        response.raise_for_status()

        for line in response.iter_lines():
            if not line:
                continue

            event = json.loads(line.decode("utf-8"))
            message = event.get("message", {})

            thinking = message.get("thinking", "")
            content = message.get("content", "")

            if thinking:
                print(thinking, end="", flush=True)
                last_output = time.monotonic()

            if content:
                print(content, end="", flush=True)
                last_output = time.monotonic()

            if event.get("done"):
                print("\n\nZavršeno.")
                print(
                    "Ukupno vreme:",
                    round(time.monotonic() - start, 2),
                    "sekundi",
                )
                print("Statistika:", {
                    k: event.get(k)
                    for k in (
                        "load_duration",
                        "prompt_eval_count",
                        "eval_count",
                        "eval_duration",
                        "done_reason",
                    )
                })
                break

except requests.RequestException as exc:
    print(f"\nHTTP/API greška: {exc}")
except KeyboardInterrupt:
    print("\nPrekinuto korisničkom komandom.")
