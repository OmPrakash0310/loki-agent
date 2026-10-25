"""Loki as a GitHub App: a customer installs it on the repos they choose, by clicking.

The operator registers one GitHub App (read-only: Metadata, Contents, Deployments,
Pull requests) and gives Loki its id and private key. A customer installs it from the
setup page; GitHub sends back an installation id. To read that customer's repos, Loki
signs a 10-minute JWT with the app's private key (RS256) and exchanges it for a 1-hour
installation token, renewed shortly before it expires. No token is ever pasted.

Env (operator): GITHUB_APP_ID, GITHUB_APP_SLUG (the app's URL name),
GITHUB_APP_PRIVATE_KEY (PEM; "\\n" escapes are fine) or GITHUB_APP_PRIVATE_KEY_FILE,
GITHUB_API_URL for GitHub Enterprise.
"""
import base64
import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

import httpx

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_tokens: dict[int, tuple[float, str]] = {}  # installation id -> (expires at, token)
_lock = threading.Lock()


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def configured() -> bool:
    return bool(_env("GITHUB_APP_ID") and _env("GITHUB_APP_SLUG") and _private_key_pem())


def _private_key_pem() -> str:
    path = _env("GITHUB_APP_PRIVATE_KEY_FILE")
    if path:
        with open(path) as f:
            return f.read()
    return _env("GITHUB_APP_PRIVATE_KEY").replace("\\n", "\n")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def app_jwt(now: float | None = None) -> str:
    """A JWT identifying the app (not a customer), valid 9 minutes (GitHub allows 10)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # iat 60s in the past absorbs clock drift between this server and GitHub.
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": _env("GITHUB_APP_ID")}).encode())
    key = serialization.load_pem_private_key(_private_key_pem().encode(), password=None)
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def _api() -> str:
    return (_env("GITHUB_API_URL") or API).rstrip("/")


def installation_token(installation_id: int, client: httpx.Client | None = None) -> str:
    with _lock:
        hit = _tokens.get(installation_id)
        if hit and hit[0] - time.time() > 300:  # renew 5 minutes before it expires
            return hit[1]
    http = client or httpx.Client(timeout=15)
    try:
        r = http.post(f"{_api()}/app/installations/{installation_id}/access_tokens",
                      headers={**HEADERS, "Authorization": f"Bearer {app_jwt()}"})
        r.raise_for_status()
        body = r.json()
    finally:
        if client is None:
            http.close()
    expires = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00")).timestamp()
    with _lock:
        _tokens[installation_id] = (expires, body["token"])
    return body["token"]


class InstallationAuth(httpx.Auth):
    """httpx auth that sends a fresh installation token on every request."""

    def __init__(self, installation_id: int):
        self.installation_id = installation_id

    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {installation_token(self.installation_id)}"
        yield request


def client(installation_id: int) -> httpx.Client:
    return httpx.Client(base_url=_api(), headers=HEADERS, auth=InstallationAuth(installation_id), timeout=15)


def installation_repos(installation_id: int, http: httpx.Client | None = None, max_pages: int = 3) -> list[str]:
    """'owner/repo' names the customer gave the app access to (up to 300)."""
    http = http or client(installation_id)
    names, url = [], "/installation/repositories"
    for _ in range(max_pages):
        r = http.get(url, params={"per_page": 100})
        r.raise_for_status()
        names += [repo["full_name"] for repo in r.json().get("repositories", [])]
        url = r.links.get("next", {}).get("url")
        if not url:
            break
    return sorted(names)


def install_url(state: str) -> str:
    return f"https://github.com/apps/{_env('GITHUB_APP_SLUG')}/installations/new?" + urlencode({"state": state})
