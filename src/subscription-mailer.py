#!/usr/bin/env python3
"""Send requested subscription confirmations independently of the newsletter relay."""

import time

from config import load_config
from subscriptions import initialize_database, send_next_confirmation


if __name__ == "__main__":
    config = load_config()
    initialize_database(config)
    while True:
        try:
            send_next_confirmation(config)
        except Exception as error:
            print(f"Confirmation worker error ({type(error).__name__})", flush=True)
        time.sleep(10)
