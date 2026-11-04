# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web Push sender (D125): message encryption (RFC 8291, the aes128gcm coding of RFC 8188),
VAPID sign-in (RFC 8292) and the push request (RFC 8030).

Built on cryptography, PyJWT and requests, which allauth already brings in, rather than on
pywebpush (D125 has the comparison). `tests/adapters/test_webpush.py` checks the encryption
byte-for-byte against the RFC examples. `WebPushSender` is the real sender; `FakePushSender`
records messages for tests, which never reach a real push service.

Nothing here logs. Results carry a short reason (status and the push service's reason
word) and never the endpoint, which is a per-device secret.
"""

from __future__ import annotations

import base64
import enum
import ipaddress
import json
import os
import re
import struct
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import jwt
import requests
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from urllib3.exceptions import LocationParseError, LocationValueError
from urllib3.util import parse_url

# RFC 8291 §4: one record of at most 4096 bytes, so at most 3993 bytes of message.
MAX_PLAINTEXT = 3993
RECORD_SIZE = 4096
# The push services browsers use today (D125). A subscription's endpoint must be on one of
# these, or a subdomain, so the server never posts to an address a browser made up.
DEFAULT_HOSTS = (
    "push.apple.com",
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "notify.windows.com",
)
URGENCIES = ("very-low", "low", "normal", "high")
# RFC 8292 lets a token last up to a day; Apple asks for a new one at most hourly.
TOKEN_LIFETIME = 12 * 60 * 60
TOKEN_REUSE = 60 * 60
MAX_TOKENS = 64  # push-service origins a sender keeps tokens for; a real site uses a handful
REASON_BYTES = 4096  # of an answer's body read at most; a longer one is cut off
# Seconds to connect, then to wait for each read. A hung push service holds the worker (and
# any solve queued behind it) for at most about this long per device.
TIMEOUT = (5.0, 10.0)
# A push endpoint in plain ASCII: https, a host of letters, digits, dots and hyphens, the
# default port at most, and a path. No backslash, space, control or non-ASCII character, no
# user name and no fragment, so every URL parser agrees on the host (D125).
_ENDPOINT = re.compile(
    r"https://(?=[A-Za-z0-9.-]{1,253}(?::443)?/)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]{0,62}"
    r"(?::443)?/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*"
)


def b64url_decode(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(hashes.SHA256(), length, salt, info).derive(ikm)


def _point(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


class SubscriptionError(ValueError):
    """A subscription's keys don't check out: the device's fault, not the server's."""


@dataclass(frozen=True)
class Subscription:
    """What a browser hands over when push is turned on: the endpoint and its two keys
    (base64url, as `PushSubscription.toJSON()` gives them)."""

    endpoint: str
    p256dh: str
    auth: str

    def keys(self) -> tuple[bytes, bytes]:
        """The decoded keys, checked: a P-256 public point and a 16-byte secret."""
        try:
            public, secret = b64url_decode(self.p256dh), b64url_decode(self.auth)
        except ValueError as exc:
            raise SubscriptionError("subscription keys aren't base64url") from exc
        if len(public) != 65 or public[0] != 0x04 or len(secret) != 16:
            raise SubscriptionError("subscription keys have the wrong length")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
        except ValueError as exc:
            raise SubscriptionError("subscription key isn't a P-256 point") from exc
        return public, secret


def record(ikm: bytes, salt: bytes, plaintext: bytes, keyid: bytes = b"", rs: int = RECORD_SIZE) -> bytes:
    """One aes128gcm record with its header (RFC 8188 §2): the last record, padded only by
    its delimiter."""
    if len(salt) != 16:
        raise ValueError("salt must be 16 bytes")
    if len(plaintext) + 1 + 16 > rs:
        raise ValueError("message too large for one record")
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    header = salt + struct.pack("!IB", rs, len(keyid)) + keyid
    return header + AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    as_private: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """Encrypt a push message for one subscription (RFC 8291 §3). A new key pair and salt
    for every message; the two keyword arguments exist only to replay the RFC example."""
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError(f"push message over {MAX_PLAINTEXT} bytes")
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = as_private.exchange(ec.ECDH(), ua_key)
    as_public = _point(as_private)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    return record(ikm, salt, plaintext, keyid=as_public)


# --- VAPID keys (RFC 8292) ---


def private_key_from_text(text: str) -> ec.EllipticCurvePrivateKey:
    """The server's VAPID key from settings: the raw 32-byte private number in base64url
    (what `manage.py vapid_keys` prints, and what the web-push tools use), or an unencrypted
    PEM key. Anything unusable raises ValueError, which turns push off rather than failing."""
    text = text.strip()
    if text.startswith("-----BEGIN"):
        try:
            key = serialization.load_pem_private_key(text.encode("ascii"), password=None)
        except (TypeError, UnicodeEncodeError, UnsupportedAlgorithm):
            # TypeError: the PEM has a passphrase. `from None` keeps the key out of tracebacks.
            raise ValueError("VAPID key must be an unencrypted P-256 key") from None
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("VAPID key must be an unencrypted P-256 key")
        return key
    raw = b64url_decode(text)
    if len(raw) != 32:
        raise ValueError("VAPID key must be 32 bytes")
    return ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())


def public_key_text(key: ec.EllipticCurvePrivateKey) -> str:
    """The public key browsers need to subscribe (`applicationServerKey`), base64url."""
    return b64url_encode(_point(key))


def generate_vapid_key() -> str:
    """A new VAPID private key as base64url text."""
    key = ec.generate_private_key(ec.SECP256R1())
    return b64url_encode(key.private_numbers().private_value.to_bytes(32, "big"))


def audience(endpoint: str) -> str:
    """The origin of the push service (RFC 8292 §2): the JWT's `aud`, serialised the way
    RFC 6454 does (lower-case host, no default port)."""
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").rstrip(".")
    port = "" if parts.port in (None, 443) else f":{parts.port}"
    return f"{parts.scheme}://{host}{port}"


def host_entry(entry: str) -> str:
    """A push host from settings, normalised: "*.push.apple.com" means push.apple.com and
    its subdomains, which is what a plain entry already means."""
    return entry.strip().lower().removeprefix("*.").strip(".")


def endpoint_allowed(endpoint: str, hosts: Iterable[str] = DEFAULT_HOSTS) -> bool:
    """True when the endpoint is a plain https URL on one of the push services (or a
    subdomain), on the default port, with no user name, password or IP address. The host is
    read by both the standard library and urllib3 (which requests connects with), and they
    must agree."""
    if not isinstance(endpoint, str) or not _ENDPOINT.fullmatch(endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        connects_to = (parse_url(endpoint).host or "").lower().rstrip(".")
    except (ValueError, LocationParseError):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host != connects_to or parts.username or parts.password or port not in (None, 443):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return any(host == h or host.endswith("." + h) for h in (host_entry(h) for h in hosts) if h)


def subject_ok(subject: str) -> bool:
    """A VAPID contact Apple accepts: mailto: or https:, and not a localhost address."""
    s = subject.strip().lower()
    if s.startswith("mailto:"):
        domain = s.rpartition("@")[2]
        return bool(domain) and domain != "localhost" and not domain.endswith(".localhost")
    if s.startswith("https://"):
        host = (urlsplit(s).hostname or "").lower()
        return (
            bool(host) and host not in ("localhost", "127.0.0.1", "::1") and not host.endswith(".localhost")
        )
    return False


# --- Sending (RFC 8030) ---


class Outcome(enum.Enum):
    SENT = "sent"
    GONE = "gone"  # 404/410: the subscription has ended; drop it
    REJECTED = "rejected"  # the push service refused this request; count it against the device
    RETRY = "retry"  # 429, 5xx or no answer: try again later


@dataclass(frozen=True)
class Message:
    payload: bytes
    ttl: int
    urgency: str = "normal"


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    status: int | None = None
    retry_after: int | None = None
    reason: str = ""


class PushSender(Protocol):
    def send(self, subscription: Subscription, message: Message) -> Result: ...


def _retry_after(value: str | None) -> int | None:
    if value and value.strip().isdigit():
        return int(value.strip())
    return None


class WebPushSender:
    """Sends one push message per call. Keeps one signed token per push service for up to
    an hour, so Apple sees at most one new token an hour (D125)."""

    def __init__(
        self,
        private_key: str,
        subject: str,
        *,
        hosts: Iterable[str] = DEFAULT_HOSTS,
        session: requests.Session | None = None,
        timeout: float | tuple[float, float] = TIMEOUT,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = private_key_from_text(private_key)
        self._public = public_key_text(self._key)
        self._subject = subject.strip()
        self._hosts = tuple(hosts)
        self._session = session or requests.Session()
        self._timeout = timeout
        self._clock = clock
        self._tokens: dict[str, tuple[float, str]] = {}

    def authorization(self, endpoint: str) -> str:
        aud = audience(endpoint)
        now = self._clock()
        cached = self._tokens.pop(aud, None)  # put back below: the dict runs least recent first
        if cached is None or now - cached[0] >= TOKEN_REUSE:
            claims = {"aud": aud, "exp": int(now) + TOKEN_LIFETIME, "sub": self._subject}
            cached = (now, jwt.encode(claims, self._key, algorithm="ES256"))
        self._tokens[aud] = cached
        while len(self._tokens) > MAX_TOKENS:
            del self._tokens[next(iter(self._tokens))]
        return f"vapid t={cached[1]}, k={self._public}"

    def send(self, subscription: Subscription, message: Message) -> Result:
        if not endpoint_allowed(subscription.endpoint, self._hosts):
            return Result(Outcome.REJECTED, reason="endpoint not on an allowed push service")
        check_message(message)
        public, secret = subscription.keys()
        body = encrypt(message.payload, public, secret)
        headers = {
            "Authorization": self.authorization(subscription.endpoint),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(message.ttl),
            "Urgency": message.urgency,
        }
        try:
            # Streamed, so an answer's body is read only so far. A short answer is read to the
            # end, which hands the connection back for the next push (keep-alive).
            with self._session.post(
                subscription.endpoint,
                data=body,
                headers=headers,
                timeout=self._timeout,
                allow_redirects=False,
                stream=True,
            ) as resp:
                status = resp.status_code
                answer = _read_some(resp)
                retry_after = _retry_after(resp.headers.get("Retry-After"))
        except (requests.exceptions.InvalidURL, LocationValueError):
            # An address no connection can be made to: the device's fault, not a busy service.
            return Result(Outcome.REJECTED, reason="unusable endpoint")
        except requests.RequestException as exc:
            return Result(Outcome.RETRY, reason=type(exc).__name__)
        if 200 <= status < 300:
            return Result(Outcome.SENT, status)
        word = _reason_word(answer)
        reason = f"{status} {word}".strip()
        if status in (404, 410):
            return Result(Outcome.GONE, status, reason=reason)
        if status == 429 or status >= 500:
            return Result(Outcome.RETRY, status, retry_after, reason)
        return Result(Outcome.REJECTED, status, reason=reason)


def _read_some(resp: requests.Response) -> bytes:
    """Up to `REASON_BYTES` of the body. Reading a short body to its end lets the connection be
    reused; a longer one is abandoned, and closing the response then drops the connection."""
    data = b""
    for chunk in resp.iter_content(1024):
        data += chunk
        if len(data) > REASON_BYTES:
            break
    return data[:REASON_BYTES]


def check_message(message: Message) -> None:
    """A message the server built wrongly (settings, not the device): raise ValueError. A TTL
    of 0 is allowed (RFC 8030 §5.2: deliver now or not at all)."""
    if message.urgency not in URGENCIES or message.ttl < 0 or len(message.payload) > MAX_PLAINTEXT:
        raise ValueError("push message, urgency or TTL out of range")


def _reason_word(body: bytes) -> str:
    """Apple and Google put a short reason in the JSON body ({"reason": "BadJwtToken"}).
    Only that word is kept, at most 40 characters, letters only."""
    try:
        data = json.loads(body)
    except ValueError:
        return ""
    word = data.get("reason", "") if isinstance(data, dict) else ""
    return "".join(c for c in str(word) if c.isalnum())[:40]


@dataclass
class FakePushSender:
    """For tests: records each message and answers with a set outcome per endpoint."""

    outcomes: dict[str, Result] = field(default_factory=dict)
    sent: list[tuple[Subscription, Message]] = field(default_factory=list)

    def send(self, subscription: Subscription, message: Message) -> Result:
        check_message(message)
        subscription.keys()
        self.sent.append((subscription, message))
        return self.outcomes.get(subscription.endpoint, Result(Outcome.SENT, 201))
