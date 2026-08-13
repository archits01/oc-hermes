import asyncio
from .server import main as async_main

def main():
    """Entrypoint that runs the async main function"""
    asyncio.run(async_main())

__all__ = ["main"]
