import asyncio
import bot

async def run_once():
    bot.load_state()
    provider = bot.Provider()
    ton_balance = await bot.get_ton_balance(provider)
    results = []
    for symbol, info in bot.TOKENS.items():
        r = await bot.handle_token(symbol, info, ton_balance)
        if r:
            results.append(r)
    bot.save_state()
    actions = [r["action"] for r in results if r["action"]]
    if actions:
        await bot.notify("⚡ تنفيذات:\n" + "\n".join(actions))

if __name__ == "__main__":
    asyncio.run(run_once())
