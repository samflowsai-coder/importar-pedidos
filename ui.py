import os

from dotenv import load_dotenv

load_dotenv()

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.web.server:app",
        host=os.getenv("PORTAL_HOST", "127.0.0.1"),
        port=int(os.getenv("PORTAL_PORT", "3636")),
        reload=os.getenv("PORTAL_RELOAD", "false").lower() == "true",
    )
