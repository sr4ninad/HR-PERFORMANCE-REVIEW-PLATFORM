from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import SEED_DEMO_DATA, STATIC_DIR
from app.csrf import csrf_cookie_middleware, verify_csrf
from app.flash import flash_middleware
from app.database import SessionLocal, engine
from app.db_migrations import upgrade_database
from app.routers import account, assignments, audit, auth, cycles, dashboard, reviews, team, users
from app.seed import seed_if_empty
from app.template_utils import templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    upgrade_database(engine)
    if SEED_DEMO_DATA:
        db = SessionLocal()
        try:
            seed_if_empty(db)
        finally:
            db.close()
    yield


# verify_csrf is app-wide so no POST route can forget it; it's a no-op on
# safe methods.
app = FastAPI(title="HR Review App", lifespan=lifespan, dependencies=[Depends(verify_csrf)])

app.middleware("http")(csrf_cookie_middleware)
app.middleware("http")(flash_middleware)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Keeps password-reset tokens in the URL path from leaking to other
    # origins (e.g. the font CDN) via the Referer header.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


app.include_router(auth.router)
app.include_router(account.router)
app.include_router(dashboard.router)
app.include_router(cycles.router)
app.include_router(assignments.router)
app.include_router(reviews.router)
app.include_router(users.router)
app.include_router(team.router)
app.include_router(audit.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    # Browsers ask for /favicon.ico regardless of the <link rel="icon">.
    return RedirectResponse("/static/favicon.svg", status_code=301)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return RedirectResponse("/login", status_code=303)
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        detail = exc.detail if exc.detail != "Forbidden" else None
        return templates.TemplateResponse(
            request, "forbidden.html", {"detail": detail}, status_code=status.HTTP_403_FORBIDDEN
        )
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        return templates.TemplateResponse(
            request, "not_found.html", {}, status_code=status.HTTP_404_NOT_FOUND
        )
    return templates.TemplateResponse(
        request, "error.html", {"detail": exc.detail}, status_code=exc.status_code
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # A missing/garbled form field on an HTML form: render a page, not
    # FastAPI's default JSON 422.
    return templates.TemplateResponse(
        request,
        "error.html",
        {"detail": "Some required form fields were missing or invalid. Go back and try again."},
        status_code=status.HTTP_400_BAD_REQUEST,
    )
