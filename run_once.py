import asyncio
import time
from bot import run_cycle

MAX_RUNTIME = 5 * 3600 + 50 * 60

async def main():
    start = time.time()
    while time.time() - start < MAX_RUNTIME:
        try:
            await run_cycle()
        except Exception as e:
            print(f"Error: {e}")
        await asyncio.sleep(15)

asyncio.run(main())
