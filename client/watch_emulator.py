import argparse
import asyncio
import json
import sys
import websockets


async def run_emulator(url: str, mac: str, name: str, api_key: str, wav_path: str | None, send_tg: bool):
    print(f"Connecting to {url} as {mac} ({name})...")
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({
            "type": "register",
            "mac": mac,
            "name": name,
            "device_type": "wear_os",
            "firmware": "1.0.0-emu",
            "api_key": api_key,
        }))

        last_text = ""

        async def receiver():
            nonlocal last_text
            async for msg in ws:
                if isinstance(msg, bytes):
                    print(f"[WARN] Unexpected binary bytes: {len(msg)}")
                    continue
                data = json.loads(msg)
                mtype = data.get("type")
                if mtype == "set_config":
                    cfg = data.get("config", {})
                    print(
                        "[CONFIG] LED:",
                        cfg.get("led_color_idle"),
                        cfg.get("led_color_listen"),
                        cfg.get("led_color_think"),
                        cfg.get("led_color_speak"),
                        "user_id:",
                        cfg.get("user_id"),
                    )
                elif mtype == "ready":
                    print(f"[READY] user={data.get('user_name') or 'auto'} has_telegram={data.get('has_telegram')}")
                elif mtype == "text_delta":
                    delta = data.get("text", "")
                    last_text += delta
                    print(delta, end="", flush=True)
                elif mtype == "turn_complete":
                    print(f"\n[TURN COMPLETE] {data.get('text', '')}")
                    if send_tg and (data.get("text") or last_text):
                        await ws.send(json.dumps({"type": "send_to_tg", "text": data.get("text") or last_text}))
                    elif not send_tg:
                        break
                elif mtype == "tg_sent":
                    print(f"[TG SENT] ok={data.get('ok')} recipient={data.get('recipient')} error={data.get('error')}")
                    break
                else:
                    print(f"[EVENT] {data}")

        recv_task = asyncio.create_task(receiver())
        await asyncio.sleep(0.4)

        if wav_path:
            import wave
            with wave.open(wav_path, "rb") as wf:
                while True:
                    chunk = wf.readframes(512)
                    if not chunk:
                        break
                    await ws.send(chunk)
                    await asyncio.sleep(0.032)
            await ws.send(json.dumps({"type": "end_of_speech"}))
        else:
            print("Connected (probe/idle mode). Sending ping...")
            await ws.send(json.dumps({"type": "ping"}))
            await asyncio.sleep(1.0)
            recv_task.cancel()
            return

        await recv_task


def main():
    parser = argparse.ArgumentParser(description="Porfiriy Wear OS Watch Emulator")
    parser.add_argument("--url", default="ws://192.168.1.50:8765")
    parser.add_argument("--mac", default="watch_emu01")
    parser.add_argument("--name", default="Pixel Watch 2 (Emulator)")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--wav", default=None, help="Optional 16kHz mono PCM WAV file to stream")
    parser.add_argument("--tg", action="store_true", help="Send response to Telegram on turn_complete")
    args = parser.parse_args()
    try:
        asyncio.run(run_emulator(args.url, args.mac, args.name, args.api_key, args.wav, args.tg))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
