import asyncio
import argparse
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main(ws_url):
    logger.info(f"Connecting debug client to {ws_url}...")
    # TODO: Initialize PyAudio
    # TODO: Initialize webrtcvad
    # TODO: Connect to WebSocket server
    pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Porfiriy Debug Audio Client")
    parser.add_argument("--url", type=str, default="ws://localhost:8765", help="Backend WebSocket URL")
    args = parser.parse_args()
    
    asyncio.run(main(args.url))
