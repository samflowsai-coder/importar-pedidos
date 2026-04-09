from dotenv import load_dotenv

load_dotenv()

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.web.server:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )
