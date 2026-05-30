import asyncio
import sys

from loguru import logger

from src.settings import settings
from src.bot import PrintBot


async def main():
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.info("Starting Asakusa Label Printer Bot")
    bot = PrintBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
