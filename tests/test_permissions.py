import pytest

from tests.conftest import login, logout

# The role x route permission matrix: who can open which page.

ADMIN_ONLY_ROUTES = [
    "/admin/dashboard",
    "/admin/cycles",
    "/admin/assignments",
    "/register",
    "/admin/audit",
]

MANAGER_ONLY_ROUTES = [
    "/manager/reports",
]

EMPLOYEE_AND_MANAGER_ROUTES = [
    "/assignments/select",
    "/reviews/pick",
]


def test_anonymous_is_redirected_to_login(client):
    for route in ["/", "/my/dashboard", *ADMIN_ONLY_ROUTES, *MANAGER_ONLY_ROUTES, *EMPLOYEE_AND_MANAGER_ROUTES]:
        r = client.get(route)
        assert r.status_code == 303
        assert r.headers["location"] == "/login"


@pytest.mark.parametrize("route", ADMIN_ONLY_ROUTES)
def test_employee_gets_403_on_admin_routes(client, route):
    login(client, "user1")
    assert client.get(route).status_code == 403


@pytest.mark.parametrize("route", ADMIN_ONLY_ROUTES)
def test_manager_gets_403_on_admin_routes(client, route):
    login(client, "manager1")
    assert client.get(route).status_code == 403


@pytest.mark.parametrize("route", ADMIN_ONLY_ROUTES)
def test_hr_admin_gets_200_on_admin_routes(client, route):
    login(client, "hr_admin", "admin123")
    assert client.get(route).status_code == 200


def test_employee_gets_403_on_manager_reports(client):
    login(client, "user1")
    assert client.get("/manager/reports").status_code == 403


def test_hr_admin_gets_403_on_manager_reports(client):
    login(client, "hr_admin", "admin123")
    assert client.get("/manager/reports").status_code == 403


def test_manager_gets_200_on_manager_reports(client):
    login(client, "manager1")
    assert client.get("/manager/reports").status_code == 200


@pytest.mark.parametrize("route", EMPLOYEE_AND_MANAGER_ROUTES)
def test_hr_admin_gets_403_on_employee_review_routes(client, route):
    login(client, "hr_admin", "admin123")
    assert client.get(route).status_code == 403


@pytest.mark.parametrize("route", EMPLOYEE_AND_MANAGER_ROUTES)
def test_employee_gets_200_on_own_review_routes(client, route):
    login(client, "user1")
    assert client.get(route).status_code == 200


def test_my_dashboard_is_200_for_every_role(client):
    for username, password in [("user1", "password123"), ("manager1", "password123"), ("hr_admin", "admin123")]:
        login(client, username, password)
        assert client.get("/my/dashboard").status_code == 200
        logout(client)


# --- Review approval & final-review routes ------------------------------------------

NEW_PROTECTED_ROUTES = ["/team/approvals", "/my/review", "/team/reviews/user1"]


def test_anonymous_is_redirected_on_review_workflow_routes(client):
    for route in NEW_PROTECTED_ROUTES:
        r = client.get(route)
        assert r.status_code == 303 and r.headers["location"] == "/login", route


@pytest.mark.parametrize(
    "username,password,route,expected",
    [
        ("user1", "password123", "/team/approvals", 403),
        ("manager1", "password123", "/team/approvals", 200),
        ("hr_admin", "admin123", "/team/approvals", 200),
        ("user1", "password123", "/my/review", 200),
        ("manager1", "password123", "/my/review", 200),
        ("hr_admin", "admin123", "/my/review", 403),
        ("manager1", "password123", "/team/reviews/user1", 200),  # direct report
        ("director1", "password123", "/team/reviews/user1", 403),  # skip-level
        ("user2", "password123", "/team/reviews/user1", 403),
        ("hr_admin", "admin123", "/team/reviews/user1", 403),
        ("user1", "password123", "/reviews/view/user1", 403),  # own raw peer reviews: never
        ("hr_admin", "admin123", "/reviews/view/user1", 200),
    ],
)
def test_review_workflow_permission_matrix(client, username, password, route, expected):
    login(client, username, password)
    assert client.get(route).status_code == expected
