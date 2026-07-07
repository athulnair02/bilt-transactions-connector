"""Automated Empower authentication flow.

Auth sequence (confirmed from HAR analysis):
1. POST credentials to multiauth/authenticate → idToken JWT
2. POST idToken (+fingerprint fields) to authenticateToken → establishes session on participant domain
3. GET home/eligibility → data.samlToken (base64-encoded signed SAMLResponse XML)
4. POST SAMLResponse to sso/saml2 → csrf token + final JSESSIONID (used by all protected endpoints)
"""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any

import requests

from utils.errors import EmpowerLoginError

EMPOWER_AUTH_URL = "https://pc-api.empower-retirement.com/api/auth/multiauth/noauth/authenticate"
PARTICIPANT_BASE_URL = "https://participant.empower-retirement.com/participant-web-services/rest"
PARTICIPANT_TOKEN_URL = f"{PARTICIPANT_BASE_URL}/nonauth/authenticateToken"
PARTICIPANT_SET_ACCU_URL = f"{PARTICIPANT_BASE_URL}/nonauth/setAccu"
PARTICIPANT_ROUTE_URL = f"{PARTICIPANT_BASE_URL}/partialauth/routeDeterminationLite"
PARTICIPANT_ELIGIBILITY_URL = f"{PARTICIPANT_BASE_URL}/home/eligibility"
PARTICIPANT_LOGIN_INFO_URL = f"{PARTICIPANT_BASE_URL}/partialauth/retirementIncomeView/participantLoginInformation"
PARTICIPANT_LOG_BROWSER_URL = f"{PARTICIPANT_BASE_URL}/nonauth/logBrowserInfo"
PARTICIPANT_MFA_OPTIONS_URL = f"{PARTICIPANT_BASE_URL}/partialauth/mfa/deliveryOptions"
PARTICIPANT_MFA_SEND_URL = f"{PARTICIPANT_BASE_URL}/partialauth/mfa/createAndDeliverActivationCode"
PARTICIPANT_MFA_VERIFY_URL = f"{PARTICIPANT_BASE_URL}/partialauth/mfa/verifycode"
EMPOWER_SAML_URL = "https://pc-api.empower-retirement.com/api/empower/sso/saml2"

DEFAULT_AUTH_CACHE = Path("empower/.empower_auth_cache.json")
CACHE_TTL_SECONDS = 1800
DEFAULT_TIMEOUT = 30

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

logger = logging.getLogger(__name__)


def _device_fingerprint() -> str:
    raw = getpass.getuser() + socket.gethostname()
    return hashlib.md5(raw.encode()).hexdigest()


def _browser_headers() -> dict[str, str]:
    """Realistic browser headers so Cloudflare doesn't block us as a bot.

    Without a real User-Agent, requests sends `python-requests/x.y`, which the
    Cloudflare protection in front of participant.empower-retirement.com rejects
    with a 403 "Just a moment..." challenge page.
    """
    return {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="147", "Not.A/Brand";v="24", "Google Chrome";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def load_auth_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_auth_cache(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def _step1_get_id_token(session: requests.Session, username: str, password: str, accu: str) -> str:
    payload = {
        "userName": username,
        "password": password,
        "deviceFingerPrint": _device_fingerprint(),
        "flowName": "mfa",
        "accu": accu,
        "requestSrc": "empower_browser",
    }
    response = session.post(EMPOWER_AUTH_URL, json=payload, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        raise EmpowerLoginError(
            f"Credential authentication failed ({response.status_code}): {response.text[:400]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise EmpowerLoginError("Authenticate response is not JSON.") from exc

    id_token = body.get("idToken") if isinstance(body, dict) else None
    if not isinstance(id_token, str) or not id_token.strip():
        keys = list(body.keys()) if isinstance(body, dict) else type(body).__name__
        raise EmpowerLoginError(f"idToken missing in authenticate response. Keys: {keys}")

    return id_token


def _set_accu(session: requests.Session, accu: str) -> None:
    """Call nonauth/setAccu before authenticateToken, as the browser does."""
    headers = {
        "origin": "https://participant.empower-retirement.com",
        "referer": "https://participant.empower-retirement.com/participant/",
    }
    session.get(f"{PARTICIPANT_SET_ACCU_URL}/{accu}", headers=headers, timeout=DEFAULT_TIMEOUT)


def _mfa_flow(session: requests.Session, accu: str) -> None:
    """Interactive MFA: fetch delivery options, send code, prompt user, verify."""
    headers = {
        "origin": "https://participant.empower-retirement.com",
        "referer": "https://participant.empower-retirement.com/participant/",
    }

    opts_resp = session.get(PARTICIPANT_MFA_OPTIONS_URL, headers=headers, timeout=DEFAULT_TIMEOUT)
    if opts_resp.status_code >= 400:
        raise EmpowerLoginError(f"Failed to fetch MFA delivery options ({opts_resp.status_code}).")

    try:
        opts_body = opts_resp.json()
    except ValueError:
        opts_body = None

    # Response: {"deliverySet": [{"deliveryType": "sms:***-***-2170", "deliveryMask": "Text me: ***-***-2170", ...}]}
    delivery_set: list[dict] = []
    if isinstance(opts_body, dict):
        raw = opts_body.get("deliverySet") or []
        if isinstance(raw, list):
            delivery_set = [o for o in raw if isinstance(o, dict) and o.get("deliveryType")]

    if not delivery_set:
        raise EmpowerLoginError(
            f"No MFA delivery options returned. Raw response: {opts_resp.text[:400]}"
        )

    if len(delivery_set) == 1:
        chosen = delivery_set[0]["deliveryType"]
        logger.info("MFA: sending code via %s", delivery_set[0].get("deliveryMask", chosen))
    else:
        print("\nMFA delivery options:")
        for idx, opt in enumerate(delivery_set, 1):
            print(f"  {idx}. {opt.get('deliveryMask', opt['deliveryType'])}")
        while True:
            raw = input("Choose a delivery option: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(delivery_set):
                chosen = delivery_set[int(raw) - 1]["deliveryType"]
                break
            logger.warning("Please enter a number between 1 and %d.", len(delivery_set))

    send_resp = session.post(
        PARTICIPANT_MFA_SEND_URL,
        json={"deliveryOption": chosen, "accu": accu},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if send_resp.status_code >= 400:
        raise EmpowerLoginError(
            f"Failed to send MFA code ({send_resp.status_code}): {send_resp.text[:300]}"
        )
    logger.info("MFA code sent via %s.", chosen)

    while True:
        code = input("Enter the activation code: ").strip()
        if code:
            break
        logger.warning("Activation code cannot be empty.")

    verify_resp = session.post(
        PARTICIPANT_MFA_VERIFY_URL,
        json={"rememberDevice": True, "verificationCode": code, "flowName": "mfa"},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if verify_resp.status_code >= 400:
        raise EmpowerLoginError(
            f"MFA verification failed ({verify_resp.status_code}): {verify_resp.text[:300]}"
        )
    logger.info("MFA verification succeeded. Device registered for future logins.")


def _step2_establish_participant_session(session: requests.Session, id_token: str, accu: str) -> str | None:
    payload = {
        "deviceFingerPrint": _device_fingerprint(),
        "userAgent": _USER_AGENT,
        "language": "en-US",
        "hasLiedLanguages": False,
        "hasLiedResolution": False,
        "hasLiedOs": False,
        "hasLiedBrowser": False,
        "flowName": "mfa",
        "accu": accu,
        "requestSrc": "empower_browser",
        "idToken": id_token,
        "authProvider": "EMPOWER",
    }
    headers = {
        "origin": "https://participant.empower-retirement.com",
        "referer": "https://participant.empower-retirement.com/participant/",
    }
    response = session.post(
        PARTICIPANT_TOKEN_URL,
        json=payload,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise EmpowerLoginError(
            f"Participant session establishment failed ({response.status_code}): {response.text[:400]}"
        )
    try:
        body = response.json()
        state = body.get("state") if isinstance(body, dict) else None
    except ValueError:
        state = None

    logger.info("authenticateToken state: %s", state)
    return state


def _warm_participant_session(session: requests.Session) -> None:
    """Mirror the three calls the browser makes after authenticateToken (HAR entries 115-117)."""
    headers = {
        "origin": "https://participant.empower-retirement.com",
        "referer": "https://participant.empower-retirement.com/participant/",
    }
    for url in (PARTICIPANT_LOGIN_INFO_URL, PARTICIPANT_LOG_BROWSER_URL, PARTICIPANT_ROUTE_URL):
        resp = session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
        logger.debug("warm-up %s → %s", url.split("/rest/")[-1], resp.status_code)


def _step3_get_saml_token(session: requests.Session) -> str:
    headers = {
        "origin": "https://participant.empower-retirement.com",
        "referer": "https://participant.empower-retirement.com/participant/",
    }
    response = session.get(PARTICIPANT_ELIGIBILITY_URL, headers=headers, timeout=DEFAULT_TIMEOUT)
    if response.status_code >= 400:
        raise EmpowerLoginError(
            f"Eligibility fetch failed ({response.status_code}): {response.text[:400]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise EmpowerLoginError("Eligibility response is not JSON.") from exc

    logger.debug("home/eligibility response: %s", response.text[:2000])

    data = body.get("data") if isinstance(body, dict) else None
    saml_token = data.get("samlToken") if isinstance(data, dict) else None
    if not isinstance(saml_token, str) or not saml_token.strip():
        raise EmpowerLoginError(
            f"samlToken missing in eligibility response. "
            f"Top-level keys: {list(body.keys()) if isinstance(body, dict) else type(body).__name__}"
        )

    return saml_token


def _step4_get_csrf_and_jsessionid(session: requests.Session, saml_token: str) -> tuple[str, str]:
    response = session.post(
        EMPOWER_SAML_URL,
        files={"SAMLResponse": (None, saml_token)},
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise EmpowerLoginError(
            f"SAML SSO failed ({response.status_code}): {response.text[:400]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise EmpowerLoginError("SAML SSO response is not JSON.") from exc

    sp_header = body.get("spHeader") if isinstance(body, dict) else None
    if not isinstance(sp_header, dict):
        raise EmpowerLoginError("spHeader missing in SAML SSO response.")

    csrf = sp_header.get("csrf")
    if not isinstance(csrf, str) or not csrf.strip():
        raise EmpowerLoginError("csrf missing in SAML SSO spHeader.")

    jsessionid = response.cookies.get("JSESSIONID")
    logger.info("JSESSIONID from saml2 response: %s", jsessionid)
    if not jsessionid:
        raise EmpowerLoginError(
            "JSESSIONID cookie was not set by the SAML SSO response. "
            "The auth flow may have changed — check empower/auth.py."
        )

    # Collect all pc-api cookies — AWSALB/AWSALBCORS/__cflb are sticky-session cookies
    # needed to route requests to the same backend that holds the session.
    extra_cookies = {
        c.name: c.value
        for c in session.cookies
        if "pc-api" in c.domain and c.name != "JSESSIONID"
    }

    return csrf.strip(), jsessionid, extra_cookies


def login(
    username: str,
    password: str,
    accu: str,
    cache_path: Path = DEFAULT_AUTH_CACHE,
) -> tuple[str, str, dict[str, str]]:
    """Run the full 4-step Empower auth flow. Returns (csrf, jsessionid, extra_cookies)."""
    session = requests.Session()
    session.headers.update(_browser_headers())

    logger.info("Authenticating with Empower (step 1/4)...")
    id_token = _step1_get_id_token(session, username, password, accu)

    logger.info("Establishing participant session (step 2/4)...")
    _set_accu(session, accu)
    state = _step2_establish_participant_session(session, id_token, accu)

    if state == "ACTIVATION_CODE_INFO":
        logger.info("MFA required (new device). Sending activation code...")
        _mfa_flow(session, accu)

    _warm_participant_session(session)

    logger.info("Fetching SAML token (step 3/4)...")
    saml_token = _step3_get_saml_token(session)

    logger.info("Completing SSO to obtain csrf + session (step 4/4)...")
    csrf, jsessionid, extra_cookies = _step4_get_csrf_and_jsessionid(session, saml_token)

    save_auth_cache(
        cache_path,
        {
            "csrf": csrf,
            "jsessionid": jsessionid,
            "extra_cookies": extra_cookies,
            "username": username,
            "accu": accu,
            "updated_at": int(time.time()),
        },
    )
    logger.info("Session tokens cached to %s.", cache_path)

    return csrf, jsessionid, extra_cookies


def ensure_empower_auth(
    cache_path: Path = DEFAULT_AUTH_CACHE,
    *,
    force: bool = False,
) -> tuple[str, str, dict[str, str]]:
    """Return (csrf, jsessionid, extra_cookies), using cache if fresh, otherwise re-authenticating."""
    if not force:
        cache = load_auth_cache(cache_path)
        jsessionid = cache.get("jsessionid")
        csrf = cache.get("csrf")
        extra_cookies = cache.get("extra_cookies") or {}
        updated_at = cache.get("updated_at", 0)
        age = time.time() - (updated_at if isinstance(updated_at, (int, float)) else 0)
        if (
            isinstance(jsessionid, str) and jsessionid
            and isinstance(csrf, str) and csrf
            and age < CACHE_TTL_SECONDS
        ):
            logger.info("Using cached Empower session tokens (age: %ds).", int(age))
            return csrf, jsessionid, extra_cookies if isinstance(extra_cookies, dict) else {}

    prior = load_auth_cache(cache_path) if not force else {}

    def _prompt(label: str, cached: str | None, *, secret: bool = False) -> str:
        default_note = f" [{cached}]" if cached else ""
        if secret:
            value = getpass.getpass(f"{label}: ")
        else:
            value = input(f"{label}{default_note}: ").strip()
            if not value and cached:
                return cached
        if not value:
            raise EmpowerLoginError(f"{label} cannot be empty.")
        return value

    username = _prompt("Empower username", prior.get("username") if isinstance(prior.get("username"), str) else None)
    password = _prompt("Empower password", None, secret=True)
    accu = _prompt("Empower plan/employer code (e.g. TriNet, Empower)", prior.get("accu") if isinstance(prior.get("accu"), str) else None)

    return login(username, password, accu, cache_path=cache_path)
