import asyncio
import websockets

async def test():
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        # Load a dummy h264 file
        # We need a dummy h264. I'll download one or just see if the server crashes.
        # But wait, we just want to know why it's not detecting.
        print("Connected")
        # I'll wait for a bit
        await asyncio.sleep(1)

asyncio.run(test())
