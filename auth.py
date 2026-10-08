"""
Credentials and access-token management.

state.json holds what you paste from the browser: the refresh token plus the
datadome / cf_clearance cookies. token.json holds the short-lived access token
derived from it. The vinted-token-refresher container writes the same two files,
so both setups work:

  - refresher running   -> token.json is always fresh, this module does nothing
  - reposter standalone -> this module refreshes the token itself when needed

Only one of the two should ever rotate the refresh token, because Vinted rotates
it on every call and invalidates the previous one. That is why SELF_REFRESH
defaults to "auto": refresh only when token.json is missing or nearly expired,
which never happens while the refresher is doing its hourly round.
"""
import contextlib
import fcntl
import json
import os
import time

import config
import vinted_api as api
from store import log

# Refresh a token that has less than this left, and treat it as expired.
EXPIRY_MARGIN = 300

# Vinted rotates the refresh token on every use and kills the previous one. If
# our reply gets lost in transit the rotation already happened server-side, so
# the token we still hold is dead. Keeping the last few means one lost response
# is not the end of the chain.
KEPT_REFRESH_TOKENS = 3

ACCESS_TOKEN_FALLBACK_TTL = 7200        # only used if the JWT carries no exp

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Shown in the Settings tab, in this order. Anything else that gets pasted --
# v_udt, anon_id, whatever Vinted starts wanting next -- is stored and sent as
# well: every string in state.json travels as a cookie, exactly as the refresher
# container does it. access_token_web is not optional: the refresh endpoint
# identifies the session by it and answers a bare 400 without it, however valid
# the refresh token is. That one took an evening to find.
CREDENTIAL_FIELDS = ("refresh_token_web", "access_token_web", "datadome",
                     "v_udt", "anon_id", "cf_clearance")

# Keys that live in state.json (or arrive in the same POST) but are never cookies.
NON_COOKIE_KEYS = {"previous_refresh_tokens", "refresh", "x_csrf_token"}


def cookie_fields(state):
    """Every stored string except the bookkeeping -- the set sent on a refresh."""
    return {k: v for k, v in state.items()
            if k not in NON_COOKIE_KEYS and isinstance(v, str) and v.strip()}


class AuthError(RuntimeError):
    pass


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


@contextlib.contextmanager
def refresh_lock():
    """
    Only one refresh at a time. A file lock rather than a threading lock,
    because the competitor is usually the vinted-token-refresher container on
    the same state volume: two refreshes in parallel means one of them rotates
    the other's token away and kills the chain.
    """
    os.makedirs(os.path.dirname(config.STATE_PATH) or ".", exist_ok=True)
    with open(config.STATE_PATH + ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# ---- credentials ---------------------------------------------------------

def load_state():
    if not os.path.exists(config.STATE_PATH):
        return {}
    try:
        with open(config.STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARN: {config.STATE_PATH} unreadable ({e}).")
        return {}


def save_state(state):
    _atomic_write(config.STATE_PATH, state)


def update_credentials(values):
    """
    Merge pasted credentials into state.json. Empty values leave the stored one
    alone, so you can update just the datadome cookie without re-pasting the
    refresh token.
    """
    state = load_state()
    changed = []
    for field, raw in (values or {}).items():
        if field in NON_COOKIE_KEYS or not isinstance(raw, str):
            continue
        new = raw.strip()
        if new and new != state.get(field):
            state[field] = new
            changed.append(field)
    if changed:
        save_state(state)
        log(f"Credentials updated: {', '.join(changed)}.")
    return changed


def credentials_status():
    """Never returns the secrets themselves -- only whether they are set."""
    state = load_state()
    out = {}
    extras = [k for k in cookie_fields(state) if k not in CREDENTIAL_FIELDS]
    for field in list(CREDENTIAL_FIELDS) + sorted(extras):
        value = state.get(field) or ""
        out[field] = {
            "set": bool(value),
            "length": len(value),
            "hint": f"…{value[-8:]}" if len(value) > 8 else "",
        }
    out["refresh_token_web"]["expires_in"] = refresh_token_expires_in()
    out["refresh_token_web"]["spares"] = len(state.get("previous_refresh_tokens") or [])
    return out


def refresh_token_expires_in():
    """
    Seconds left on the refresh token, or None. It lives about a week, and when
    it dies the only cure is pasting fresh cookies -- so this is worth showing
    before it happens rather than after.
    """
    token = load_state().get("refresh_token_web")
    expires = api.jwt_expiry(token) if token else None
    return int(expires - time.time()) if expires else None


def remember_cookies(cookies):
    """
    Store a datadome / cf_clearance that Vinted handed out during a normal call.
    Called from the API client, so the cookie stays current instead of ageing
    between token refreshes until it trips a CAPTCHA.
    """
    state = load_state()
    changed = [k for k, v in cookies.items() if v and state.get(k) != v]
    if not changed:
        return
    state.update({k: cookies[k] for k in changed})
    save_state(state)
    token = load_token()
    if token:                                   # keep the two files in step
        token.update({k: cookies[k] for k in changed})
        _atomic_write(config.TOKEN_PATH, token)
    log(f"Refreshed cookie stored: {', '.join(changed)}.")


# ---- access token --------------------------------------------------------

def load_token():
    if not os.path.exists(config.TOKEN_PATH):
        return None
    try:
        with open(config.TOKEN_PATH) as f:
            token = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    return token if token.get("access_token") else None


def token_is_fresh(token):
    if not token:
        return False
    expires = token.get("expires_at")
    return bool(expires) and expires - time.time() > EXPIRY_MARGIN


def _post_refresh(state, refresh_token):
    """One attempt with one refresh token. Returns the parsed body and session."""
    s = api.new_session()
    s.headers.update({
        "User-Agent": config.USER_AGENT,
        **BASE_HEADERS,
        **api.client_hints(config.USER_AGENT),
        "Origin": f"https://{config.LOCALE}",
        "Referer": f"https://{config.LOCALE}/",
    })
    # Everything stored goes along as a cookie. access_token_web in particular:
    # the refresh endpoint identifies the session by it, and without it Vinted
    # answers a bare 400 however valid the refresh token is.
    cookies = cookie_fields(state)
    cookies["refresh_token_web"] = refresh_token
    s.cookies.update(cookies)

    r = s.post(f"https://{config.LOCALE}/web/api/auth/refresh", timeout=20)
    if r.status_code != 200:
        raise AuthError(f"refresh returned {r.status_code}: {r.text[:200]}")
    body = r.json()
    if not body.get("access_token"):
        raise AuthError(f"no access_token in response: {r.text[:200]}")
    return body, s


def refresh_now(_locked=False):
    """
    Trade the refresh token for a new access token. Vinted hands back a rotated
    refresh token, which is written straight back to state.json -- lose it and
    the chain is dead.

    If the current token is refused, the previously rotated ones are tried: a
    refresh whose response never reached us leaves us holding a token Vinted has
    already retired, and the one before it is then still the live one.
    """
    if not _locked:
        with refresh_lock():
            return refresh_now(_locked=True)

    state = load_state()
    if not state.get("refresh_token_web"):
        raise AuthError("no refresh_token_web stored -- paste it under Settings.")

    candidates = [state["refresh_token_web"]] + list(state.get("previous_refresh_tokens") or [])
    body = session = None
    for n, candidate in enumerate(candidates):
        try:
            body, session = _post_refresh(state, candidate)
            if n:
                log(f"The stored refresh token was refused; fell back to spare {n}.")
            break
        except AuthError as e:
            if n == len(candidates) - 1:
                raise AuthError(f"{e} (tried {len(candidates)} stored refresh token(s))")
            continue

    rotated = (body.get("refresh_token")
               or api.cookie_value(session.cookies, "refresh_token_web", config.LOCALE)
               or state["refresh_token_web"])
    spares = [t for t in [state["refresh_token_web"]] +
              list(state.get("previous_refresh_tokens") or [])
              if t and t != rotated]
    state["previous_refresh_tokens"] = spares[:KEPT_REFRESH_TOKENS]
    state["refresh_token_web"] = rotated
    for name in ("datadome", "cf_clearance"):
        state[name] = api.cookie_value(session.cookies, name, config.LOCALE) or state.get(name)
    save_state(state)

    access_token = body["access_token"]
    expires_at = api.jwt_expiry(access_token) or int(time.time()) + ACCESS_TOKEN_FALLBACK_TTL
    state["access_token_web"] = access_token      # needed by the next refresh
    save_state(state)
    token = {
        "access_token": access_token,
        "obtained_at": int(time.time()),
        "expires_at": expires_at,
        "datadome": state.get("datadome"),
        "cf_clearance": state.get("cf_clearance"),
    }
    _atomic_write(config.TOKEN_PATH, token)
    log(f"Access token refreshed, valid for {int((expires_at - time.time()) / 60)} min; "
        f"rotated refresh token saved.")
    return token


KEEPALIVE_MARGIN = 6 * 86400       # a refresh token lives 7 days: act once a day is gone


def keep_alive():
    """
    Keep the login itself alive, whether or not anything else is happening.

    The refresh token dies seven days after it was last used, and the only cure
    then is pasting fresh cookies. Everything else here refreshes on demand --
    when a job runs, when the page loads the wardrobe -- so a container that
    sits idle for a week (no auto-backup, no automatic reposting, nobody opening
    the page) would quietly lose its login. This is the one call that does not
    depend on being used.

    It does nothing while the access token is fresh, which is always the case
    when the token refresher container shares this state folder: the two never
    compete. Returns what it did; raises AuthError when the login is beyond
    saving or the refresh was refused.
    """
    if config.SELF_REFRESH == "never":
        return "off"
    if not load_state().get("refresh_token_web"):
        return "no credentials"
    with refresh_lock():
        if token_is_fresh(load_token()):
            return "fresh"                      # refreshed within the last two hours
        left = refresh_token_expires_in()
        if left is not None and left <= 0:
            raise AuthError(
                f"the Vinted login expired {int(-left / 3600)}h ago -- nothing used it for "
                f"a week. Send fresh cookies from your browser (Tampermonkey: Send to "
                f"reposter, or paste them under Settings).")
        if left is not None and left > KEEPALIVE_MARGIN:
            return "fresh"
        refresh_now(_locked=True)
        return "refreshed"


def ensure_token():
    """The access token every Vinted call runs on, refreshed if it has to be."""
    token = load_token()
    if token_is_fresh(token):
        return token

    if config.SELF_REFRESH == "never":
        raise AuthError(
            "the stored access token is missing or expired, and SELF_REFRESH is "
            "off. The vinted-token-refresher container should be writing "
            f"{config.TOKEN_PATH} -- check its logs.")

    if not load_state().get("refresh_token_web"):
        raise AuthError(
            f"no usable access token in {config.TOKEN_PATH} and no refresh token "
            f"stored. Paste your browser cookies under Settings, or point this "
            f"container at the refresher's state volume.")

    with refresh_lock():
        # somebody else may have refreshed while we waited for the lock
        token = load_token()
        if token_is_fresh(token):
            return token
        return refresh_now(_locked=True)
