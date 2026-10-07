import os
import re
import tempfile

# Keep the test suite from touching the real dev hr_review.db file: the
# app's startup event migrates and seeds through app.database.engine
# directly (not through the overridable get_db dependency), so redirect
# that engine to a throwaway on-disk database before app.main is imported.
# (A bare sqlite:///:memory: won't work here: TestClient dispatches requests
# on a worker thread different from the one that ran the migrations, and
# each new connection to an in-memory sqlite DB starts out empty.)
_startup_fd, _startup_db_path = tempfile.mkstemp(suffix=".db")
os.close(_startup_fd)
os.environ.setdefault("HR_REVIEW_DATABASE_URL", f"sqlite:///{_startup_db_path}")
# Fixed key so importing app.security doesn't generate a .secret_key file
# in the project directory.
os.environ.setdefault("HR_REVIEW_SECRET_KEY", "test-only-secret-key-not-used-anywhere-real")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD
from app.database import Base, get_db
from app.main import app
from tests.fixture_org import seed_test_org


class CSRFAwareClient(TestClient):
    """Behaves like a browser submitting the app's own forms: every POST
    carries the csrf_token hidden field matching the cookie. Pass an
    explicit `csrf_token` in `data` to override (e.g. to test rejection)."""

    def post(self, url, *args, data=None, **kwargs):
        data = dict(data or {})
        if CSRF_FORM_FIELD not in data:
            if CSRF_COOKIE_NAME not in self.cookies:
                self.get("/login")  # any page load issues the cookie
            data[CSRF_FORM_FIELD] = self.cookies.get(CSRF_COOKIE_NAME)
        return super().post(url, *args, data=data, **kwargs)


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    session = TestingSessionLocal()
    seed_test_org(session)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with CSRFAwareClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, username, password="password123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def logout(client):
    return client.post("/logout")


def nominate_and_approve(
    client,
    reviewee="user2",
    reviewers=("user1", "user3", "user4"),
    approver=("manager1", "password123"),
):
    """The full selection step: the reviewee nominates, then their manager
    approves every pending nomination on the approvals page. Leaves the
    client logged out."""
    login(client, reviewee)
    r = client.post("/assignments/select", data={"reviewers": list(reviewers)})
    assert r.status_code == 303, r.text
    logout(client)

    login(client, *approver)
    page = client.get("/team/approvals")
    for assignment_id in re.findall(r"/team/approvals/(\d+)/approve", page.text):
        assert client.post(f"/team/approvals/{assignment_id}/approve").status_code == 303
    logout(client)
