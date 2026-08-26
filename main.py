"""
Top-level application entry point for Vercel, Uvicorn, and ASGI servers.
"""
from src.api.app import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.api.app:app", host="0.0.0.0", port=8000, reload=True)
