import asyncio
import sys

from loguru import logger

from src.settings import settings
from src.storage import Storage
from src.bot import PrintBot


async def main():
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.info("Starting Asakusa Label Printer Bot")
    storage = Storage(settings.labels_db_path)
    await storage.init()
    bot = PrintBot(storage)
    try:
        await bot.start()
    finally:
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
