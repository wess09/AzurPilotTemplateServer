import logging
import os

import uvicorn


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        "templateserver.api:create_app",
        factory=True,
        host=os.getenv("LISTEN_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8093")),
    )


if __name__ == "__main__":
    main()
