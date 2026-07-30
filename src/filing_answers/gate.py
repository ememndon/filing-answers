"""A password in front of the whole site.

Not a login. There are no accounts here, nothing personal is stored, and
nobody needs to be told apart from anybody else — so a username would be
a field to fill in that decides nothing. What this does is decide whether
a visitor gets in at all, which is the only question worth asking of a
demo that costs money to run and is meant to be handed to a named few.

Off by default. `SITE_PASSWORD` unset means no gate, so anyone who clones
this repository gets a working service rather than a locked door and a
puzzle. Twelve-factor: the deployment decides, not the code.

Four things it has to get right, none of them clever:

  compare in constant time      a naive `==` leaks the password one
                                character at a time to anyone patient
                                enough to measure the reply

  sign the cookie               otherwise the cookie *is* the password —
                                anyone can set `entered=yes` in a
                                console and walk past

  limit the guessing            a four-word password is strong against a
                                person and weak against a loop

  cover the API too             a gate on the page and an open /ask
                                endpoint is decoration; the expensive
                                part is behind the endpoint

Sessions carry an expiry inside the signature rather than being stored,
so there is no session table to keep, and the secret is derived from the
password itself — change the password and every existing session stops
working, which is what anyone changing a password expects to happen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections import defaultdict, deque

COOKIE = "filing_answers_entry"

#: How long a visitor stays in. Long enough to read the thing and come
#: back after lunch, short enough that a borrowed laptop is not a
#: standing invitation.
SESSION_HOURS = 12

#: Paths that answer before the gate. Health and readiness are for
#: orchestrators, which have no browser and no password, and a container
#: that cannot report its own health gets restarted forever.
OPEN_PATHS = frozenset({"/health", "/ready", "/enter"})


def _secret(password: str) -> bytes:
    """The signing key, derived from the password.

    Deliberately not independent of it. Changing the password should
    invalidate every session issued under the old one, and deriving the
    key means that happens by itself rather than by remembering to
    rotate a second value.
    """
    return hashlib.sha256(f"filing-answers/{password}".encode()).digest()


def issue(password: str, *, now: float | None = None, hours: int = SESSION_HOURS) -> str:
    """A signed token saying when this visitor's welcome runs out."""
    expires = int((now if now is not None else time.time()) + hours * 3600)
    payload = str(expires).encode()
    signature = hmac.new(_secret(password), payload, hashlib.sha256).digest()
    return f"{expires}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def admitted(password: str, token: str | None, *, now: float | None = None) -> bool:
    """Whether a token was issued by us and has not expired."""
    if not token or "." not in token:
        return False
    expires_raw, _, _ = token.partition(".")
    try:
        expires = int(expires_raw)
    except ValueError:
        return False

    # Signature first, then expiry. Checking expiry on an unverified
    # payload would be trusting a number the visitor chose.
    expected = issue(password, now=expires - SESSION_HOURS * 3600, hours=SESSION_HOURS)
    if not hmac.compare_digest(token, expected):
        return False
    return expires > (now if now is not None else time.time())


def correct(password: str, offered: str) -> bool:
    """Whether the password given matches, compared in constant time.

    `==` on strings returns as soon as two characters differ, and the
    time that takes is measurable across a network. Given enough
    attempts it hands over the password one character at a time.
    """
    return hmac.compare_digest(password.encode(), offered.encode())


class Attempts:
    """How many guesses one address gets.

    A password strong against a person is weak against a loop. This is
    what turns "hard to guess" into "not worth trying", and it is
    separate from the question limiter because the two count different
    things and a failed guess must not consume somebody's questions.
    """

    def __init__(self, allowed: int = 8, window: int = 900, clock=time.monotonic) -> None:
        self.allowed = allowed
        self.window = window
        self._clock = clock
        self._seen: dict[str, deque[float]] = defaultdict(deque)

    def too_many(self, caller: str) -> bool:
        now = self._clock()
        tries = self._seen[caller]
        while tries and now - tries[0] > self.window:
            tries.popleft()
        return len(tries) >= self.allowed

    def record_failure(self, caller: str) -> None:
        # Only failures count. Somebody who knows the password is not
        # attacking anything, and locking them out for revisiting the
        # page would be a limit on the wrong behaviour.
        self._seen[caller].append(self._clock())

    def forgive(self, caller: str) -> None:
        self._seen.pop(caller, None)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>filing-answers</title>
<style>
  :root {{
    --ink:#16181d; --muted:#5c636e; --line:#e3e6ea;
    --page:#f7f8fa; --card:#fff; --accent:#1f3a5f; --bad:#a32b1e;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --ink:#e8eaed; --muted:#a0a7b2; --line:#2a2f37;
             --page:#14161a; --card:#1b1e24; --accent:#9db8dc; --bad:#f0897c; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
    justify-content:center; padding:1.5rem; background:var(--page); color:var(--ink);
    font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .box {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:2rem; width:100%; max-width:24rem; }}
  h1 {{ font-size:1.25rem; margin:0 0 .4rem; }}
  p {{ color:var(--muted); margin:0 0 1.4rem; font-size:.92rem; }}
  label {{ display:block; font-size:.8rem; font-weight:600;
           color:var(--muted); margin-bottom:.3rem; }}
  input {{ font:inherit; width:100%; color:var(--ink); background:var(--page);
    border:1px solid var(--line); border-radius:7px; padding:.6rem .7rem; }}
  button {{ font:inherit; font-weight:600; cursor:pointer; width:100%; margin-top:1rem;
    background:var(--accent); color:var(--card); border:0; border-radius:7px; padding:.65rem; }}
  .err {{ color:var(--bad); font-size:.85rem; margin:.8rem 0 0; }}
</style></head>
<body>
  <div class="box">
    <h1>filing-answers</h1>
    <p>Answers questions about company annual reports, and withholds any
       answer it cannot support with a quotation from the filing.</p>
    <form method="post" action="/enter">
      <label for="p">Password</label>
      <input id="p" name="password" type="password" autofocus required
             autocomplete="current-password">
      <button type="submit">Enter</button>
      {message}
    </form>
  </div>
</body></html>
"""


def login_page(message: str = "") -> str:
    return LOGIN_PAGE.format(message=f'<p class="err">{message}</p>' if message else "")
