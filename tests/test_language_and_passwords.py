"""The profanity filter and the password-strength rules."""

import re

import pytest

from app.language_filter import find_banned_language
from app.models import SKILL_AREAS
from app.services import password_problem
from tests.conftest import login, logout, nominate_and_approve

# --- Profanity filter --------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        # swear words and their inflections / compounds
        "You fucker", "fucking hell", "fuckin lazy", "what a motherfucker", "a total clusterfuck",
        "bullshit excuses", "shitty code", "shithead", "an asshole", "dumbass move", "jackass",
        "half-assed work", "bitchy attitude", "bastards", "cunt", "dickhead", "prick", "twat",
        "wanker", "piss off", "pissed off", "crappy", "goddamn", "damned", "this sucks",
        "bloody useless",
        # disguised spellings
        "f*ck this", "FUUUCK", "sh1t work", "$hit", "@ss", "b!tch",
        # insults, slurs, slang and phrases
        "idiot", "idiots", "losers", "pathetic", "retarded", "faggot", "wtf", "STFU",
        "shut up", "screw you", "crazy",
    ],
)
def test_filter_blocks_profanity_insults_and_slurs(text):
    assert find_banned_language(text), text


@pytest.mark.parametrize(
    "text",
    [
        # normal words that contain a bad word's letters
        "They made great progress", "assess the class assignments", "assessment", "assets",
        "passing grade", "the assistant manager", "embarrass", "harass", "compass", "glasses",
        "Scunthorpe office", "cockpit software", "cocktail", "Hancock", "Dickens novel",
        "scrap the old plan", "hello team", "shell scripts", "hellenic", "title", "attitude",
        "constitution", "succeeded", "suction", "dumbbell", "snapshot", "worksheet", "bitcoin",
        # legitimate phrases
        "a chink in the armor", "POS system", "sob story",
    ],
)
def test_filter_allows_normal_words(text):
    assert find_banned_language(text) == [], text


def test_filter_reports_every_offending_word_once():
    assert find_banned_language("You fucker, this is bullshit", "and you're an idiot, fucker") == [
        "fucker", "bullshit", "idiot",
    ]


def _review_form(notes):
    form = {"additional_notes": notes}
    for area in SKILL_AREAS:
        form[f"{area}_text"] = "solid work"
        form[f"{area}_rating"] = "4"
    return form


def test_swear_word_in_additional_notes_blocks_the_review(client, db_session):
    nominate_and_approve(client)
    login(client, "user1")
    assignment_id = re.search(r"/reviews/write/(\d+)", client.get("/reviews/pick").text).group(1)

    r = client.post(f"/reviews/write/{assignment_id}", data=_review_form("He is a fucker sometimes."))
    assert r.status_code == 400
    assert "inappropriate language: fucker" in r.text

    from app.models import Review

    assert db_session.query(Review).count() == 0  # nothing saved


def test_swear_word_blocks_the_managers_final_review_too(client, db_session):
    login(client, "manager1")
    r = client.post(
        "/team/reviews/user2",
        data={"cycle_id": "1", "action": "save", "summary": "What a shitty quarter.", "final_rating": "2"},
    )
    assert r.status_code == 400
    assert "shitty" in r.text


# --- Password rules ----------------------------------------------------------------

@pytest.mark.parametrize(
    "password,missing",
    [
        ("Sh0rt!", "at least 8 characters"),
        ("alllower1!", "an uppercase letter"),
        ("ALLUPPER1!", "a lowercase letter"),
        ("NoNumbers!", "a number"),
        ("NoSpecial1", "a special character"),
    ],
)
def test_each_password_rule_is_enforced(password, missing):
    problem = password_problem(password)
    assert problem is not None and missing in problem


def test_password_error_lists_everything_missing_at_once():
    assert password_problem("abc") == (
        "Password needs at least 8 characters, an uppercase letter, a number and a special character (e.g. ! @ # $ %)."
    )


def test_strong_password_is_accepted():
    assert password_problem("Correct-Horse-9") is None


def test_password_cannot_contain_username():
    assert password_problem("Hello-user1-99", "user1") == "Password can't contain your username."
    assert password_problem("Hello-USER1-99", "user1") == "Password can't contain your username."  # case-insensitive
    assert password_problem("Hello-world-99", "user1") is None


def test_password_length_cap_still_applies():
    assert "at most" in password_problem("Aa1!" * 100)


def test_change_password_rejects_weak_and_username_passwords(client):
    login(client, "user1")
    for weak in ["password123", "User1-Rocks!9"]:
        r = client.post(
            "/account/password",
            data={"current_password": "password123", "new_password": weak, "confirm_password": weak},
        )
        assert r.status_code == 400, weak
    logout(client)
    assert login(client, "user1").status_code == 303  # the old password still works


def test_password_rules_are_shown_on_the_form(client):
    login(client, "user1")
    assert "an uppercase letter, a lowercase letter, a number and a special character" in client.get(
        "/account/password"
    ).text


def test_existing_passwords_keep_working_after_the_policy(client):
    # The demo accounts predate the rules; rules apply only when a password is set.
    assert login(client, "user2", "password123").status_code == 303
