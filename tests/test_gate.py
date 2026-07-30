"""The password in front of the site.

Access control is the one thing where a passing test is not enough — the
interesting cases are all the ways in that are *not* meant to work. Most
of what is below is an attempt to get past it.
"""

from __future__ import annotations

from filing_answers.gate import (
    SESSION_HOURS,
    Attempts,
    admitted,
    correct,
    issue,
    login_page,
)

PASSWORD = "a-real-password"


class TestCheckingThePassword:
    def test_accepts_the_right_one(self) -> None:
        assert correct(PASSWORD, PASSWORD)

    def test_rejects_a_wrong_one(self) -> None:
        assert not correct(PASSWORD, "something else entirely")

    def test_rejects_one_that_is_nearly_right(self) -> None:
        assert not correct(PASSWORD, "a-real-passwore")
        assert not correct(PASSWORD, "a-real-password ")
        assert not correct(PASSWORD, "A-Real-Password")

    def test_rejects_a_prefix(self) -> None:
        # the case a length check alone would let through
        assert not correct(PASSWORD, "a-real")
        assert not correct(PASSWORD, "")

    def test_handles_a_password_with_awkward_characters(self) -> None:
        # compare_digest works on bytes, so anything non-ASCII has to
        # survive the encoding rather than raise
        assert correct("naïve—π", "naïve—π")
        assert not correct("naïve—π", "naive-pi")


class TestTheSessionToken:
    def test_one_it_issued_is_admitted(self) -> None:
        assert admitted(PASSWORD, issue(PASSWORD))

    def test_a_token_nobody_signed_is_not(self) -> None:
        # the whole reason the cookie is signed. Without it the cookie
        # *is* the password and anyone can set one in a console.
        assert not admitted(PASSWORD, "9999999999.anything")
        assert not admitted(PASSWORD, "yes")
        assert not admitted(PASSWORD, "")
        assert not admitted(PASSWORD, None)

    def test_one_signed_with_a_different_password_is_not(self) -> None:
        assert not admitted(PASSWORD, issue("some other password"))

    def test_changing_the_password_invalidates_what_was_issued(self) -> None:
        # what anyone changing a password expects to happen, and it
        # happens by itself because the signing key is derived from it
        before = issue(PASSWORD)
        assert not admitted("the new password", before)

    def test_an_expired_token_is_not(self) -> None:
        long_ago = 1_000_000.0
        token = issue(PASSWORD, now=long_ago)
        assert admitted(PASSWORD, token, now=long_ago + 60)
        assert not admitted(PASSWORD, token, now=long_ago + SESSION_HOURS * 3600 + 1)

    def test_a_visitor_cannot_extend_their_own_stay(self) -> None:
        # editing the expiry in the cookie changes the payload, so the
        # signature no longer matches. Expiry is checked after the
        # signature for exactly this reason.
        token = issue(PASSWORD, now=1_000_000.0)
        _, _, signature = token.partition(".")
        forged = f"{99_999_999_999}.{signature}"
        assert not admitted(PASSWORD, forged)

    def test_rubbish_in_the_cookie_is_refused_and_not_a_crash(self) -> None:
        for nonsense in ["...", "abc.def", ".", "12", "nan.x", "1e9.x"]:
            assert not admitted(PASSWORD, nonsense), nonsense


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestLimitingTheGuessing:
    def test_allows_a_few_mistakes(self) -> None:
        attempts = Attempts(allowed=3, clock=Clock())
        for _ in range(2):
            attempts.record_failure("1.2.3.4")
        assert not attempts.too_many("1.2.3.4")

    def test_stops_a_loop(self) -> None:
        # a password strong against a person is weak against a script
        attempts = Attempts(allowed=3, clock=Clock())
        for _ in range(3):
            attempts.record_failure("1.2.3.4")
        assert attempts.too_many("1.2.3.4")

    def test_does_not_punish_anybody_else(self) -> None:
        attempts = Attempts(allowed=2, clock=Clock())
        for _ in range(5):
            attempts.record_failure("1.2.3.4")
        assert not attempts.too_many("5.6.7.8")

    def test_forgets_old_failures(self) -> None:
        clock = Clock()
        attempts = Attempts(allowed=2, window=900, clock=clock)
        attempts.record_failure("1.2.3.4")
        attempts.record_failure("1.2.3.4")
        assert attempts.too_many("1.2.3.4")
        clock.advance(901)
        assert not attempts.too_many("1.2.3.4")

    def test_getting_it_right_clears_the_slate(self) -> None:
        # somebody who mistyped twice and then remembered is not an
        # attacker, and should not be one failure from being locked out
        attempts = Attempts(allowed=3, clock=Clock())
        attempts.record_failure("1.2.3.4")
        attempts.record_failure("1.2.3.4")
        attempts.forgive("1.2.3.4")
        assert not attempts.too_many("1.2.3.4")


class TestTheLoginPage:
    def test_asks_for_a_password_and_posts_it(self) -> None:
        page = login_page()
        assert 'name="password"' in page
        assert 'type="password"' in page
        assert 'action="/enter"' in page

    def test_carries_a_message_when_there_is_one(self) -> None:
        assert "That is not the password." in login_page("That is not the password.")

    def test_says_nothing_when_there_is_not(self) -> None:
        assert 'class="err"' not in login_page()
