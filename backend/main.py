import asyncio
import json
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Porfiriy Backend starting...")
    
    # Read options from options.json (HA Add-on) or .env for local testing
    options_path = "/data/options.json"
    if os.path.exists(options_path):
        with open(options_path, "r") as f:
            options = json.load(f)
            logger.info("Loaded options from HA Add-on")
    else:
        options = {}
        logger.info("Running outside HA Add-on, using default/env options")
        
    # TODO: Initialize Gemini Client
    # TODO: Fetch entities from HA Supervisor API
    # TODO: Start WebSocket Server
    
    # Keep the server running
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
