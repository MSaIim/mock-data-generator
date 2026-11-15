import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time

from store import meta

# =================================================
# CONFIG
# =================================================

COOKIE_NAME = "mockgen_session"
SESSION_SECONDS = 12 * 3600
HASH_ITERATIONS = 210000
ROLES = ("user", "admin")
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 10

# Metadata keys
SESSION_KEY_PK = "CONFIG#session_key"
SETUP_PK = "CONFIG#setup"

# Session signing key, read once per process
_session_key = None

# =================================================
# HELPERS
# =================================================


# Get the session signing key, creating it on first use
def session_key() -> bytes:
    """
    Load the session signing key. The first request ever generates and stores it;
    the insert-if-absent write makes concurrent first requests agree on one key.

    Returns:
        bytes: signing key
    """
    global _session_key
    if _session_key is None:
        meta.put_new({"pk": SESSION_KEY_PK, "key": secrets.token_hex(32)})
        _session_key = meta.get(SESSION_KEY_PK)["key"].encode("utf-8")
    return _session_key


# Hash a password with PBKDF2
def hash_password(password: str, salt: bytes) -> str:
    """
    Hash a password with PBKDF2-SHA256.

    Arguments:
        password (str): plain password
        salt (bytes): random salt

    Returns:
        str: hex digest
    """
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS).hex()


# Sign a session payload
def sign(payload: str) -> str:
    """
    HMAC-sign a session payload.

    Arguments:
        payload (str): encoded payload

    Returns:
        str: hex signature
    """
    return hmac.new(session_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()


# Validate a username and password
def check_credentials(username, password) -> None:
    """
    Validate a requested username and password.

    Arguments:
        username: requested username
        password: requested password
    """
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise ValueError("Username must be 3-32 characters: lowercase letters, digits, . _ -")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


# =================================================
# FIRST-RUN SETUP
# =================================================


# Whether the first admin still has to be created
def setup_needed() -> bool:
    """
    Check whether first-run setup has been done.

    Returns:
        bool: True until the first admin is created
    """
    return meta.get(SETUP_PK) is None


# Create the first admin (works exactly once)
def setup(username, password) -> None:
    """
    Create the first admin account. The insert-if-absent write on the setup marker means
    only one request can ever succeed, even if several arrive at once.

    Arguments:
        username: admin username
        password: admin password
    """
    check_credentials(username, password)
    if not meta.put_new({"pk": SETUP_PK, "by": username, "at": int(time.time())}):
        raise ValueError("Setup is already done. Sign in instead")
    create_user(username, password, "admin")


# =================================================
# LOGIN AND SESSIONS
# =================================================


# Check a username and password
def login(username: str, password: str):
    """
    Check credentials.

    Arguments:
        username (str): username
        password (str): password

    Returns:
        str | None: the user's role when the credentials are valid, otherwise None
    """
    item = meta.get("USER#" + username)
    # Hash even when the user doesn't exist so timing doesn't reveal which usernames exist
    salt = bytes.fromhex(item["salt"]) if item else b"\x00" * 16
    digest = hash_password(password, salt)
    if item and hmac.compare_digest(digest, item["hash"]):
        return item["role"]
    return None


# Create a signed session token
def make_session(username: str) -> str:
    """
    Create a signed session token holding the username and an expiry.

    Arguments:
        username (str): logged-in username

    Returns:
        str: token for the session cookie
    """
    data = json.dumps({"u": username, "exp": int(time.time()) + SESSION_SECONDS})
    payload = base64.urlsafe_b64encode(data.encode("utf-8")).decode("ascii")
    return payload + "." + sign(payload)


# Read and verify the session cookie
def read_session(cookie_header: str):
    """
    Verify the session cookie. The role is looked up fresh on every request,
    so removed or demoted users lose access immediately.

    Arguments:
        cookie_header (str): raw Cookie header, may be None

    Returns:
        tuple | None: (username, role), or None when not logged in
    """
    # Find the session cookie
    token = None
    for part in (cookie_header or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == COOKIE_NAME:
            token = value
    if not token or "." not in token:
        return None

    # Verify signature and expiry
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature.encode("utf-8"), sign(payload).encode("utf-8")):
        return None
    data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    if data["exp"] < time.time():
        return None

    # Current role from storage
    item = meta.get("USER#" + data["u"])
    if item is None:
        return None
    return data["u"], item["role"]


# =================================================
# USER MANAGEMENT (admin only)
# =================================================


# List users
def list_users() -> list:
    """
    List users.

    Returns:
        list: dicts with username and role, sorted by username
    """
    users = []
    for item in meta.scan("USER#"):
        users.append({"username": item["username"], "role": item["role"]})
    users.sort(key=lambda u: u["username"])
    return users


# Create a user
def create_user(username, password, role) -> None:
    """
    Create a user with a salted, hashed password.

    Arguments:
        username: requested username
        password: requested password
        role: "user" or "admin"
    """
    check_credentials(username, password)
    if role not in ROLES:
        raise ValueError("Role must be user or admin")
    salt = os.urandom(16)
    item = {"pk": "USER#" + username, "username": username, "role": role, "salt": salt.hex(), "hash": hash_password(password, salt)}
    if not meta.put_new(item):
        raise ValueError(f"User {username} already exists")


# Delete a user
def delete_user(username, current_user: str) -> None:
    """
    Delete a user. Admins can't delete themselves, so at least one admin always remains.

    Arguments:
        username: username to delete
        current_user (str): the admin making the request
    """
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        raise ValueError("Invalid username")
    if username == current_user:
        raise ValueError("You can't delete your own account")
    meta.delete("USER#" + username)
