"""루트 진입점 — `uvicorn main:app` 및 `python main.py` 양쪽 지원."""
from api.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000)
