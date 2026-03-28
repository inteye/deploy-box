from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .auth import bootstrap_admin
from .config import get_settings
from .database import Base, SessionLocal, engine, ensure_schema_compatibility
from .routers import build_jobs, deployments, projects, releases, web
from .services import ensure_builtin_templates


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility()
    db = SessionLocal()
    try:
        bootstrap_admin(db)
        ensure_builtin_templates(db)
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(projects.router)
app.include_router(releases.router)
app.include_router(deployments.router)
app.include_router(build_jobs.router)
app.include_router(web.router)
