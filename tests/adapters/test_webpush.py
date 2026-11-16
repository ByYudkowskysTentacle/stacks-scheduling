# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Web Push sender (D125) against the worked examples in RFC 8291, RFC 8188 and RFC 8292,
plus its request handling against a recording transport. Nothing here reaches a real push
service."""

from __future__ import annotations

import json
import struct

import jwt
import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from hypothesis import example, given, settings
from hypothesis import strategies as st
from requests.adapters import BaseAdapter

from adapters import webpush
from adapters.webpush import (
    FakePushSender,
    Message,
    Outcome,
    Result,
    Subscription,
    WebPushSender,
    b64url_decode,
    b64url_encode,
)

# RFC 8291 §5 and Appendix A.
RFC8291_PLAINTEXT = b"When I grow up, I want to be a watermelon"
RFC8291_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC8291_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC8291_UA_PUBLIC = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
RFC8291_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"  # gitleaks:allow (the public RFC 8291 example, not a secret)
RFC8291_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC8291_BODY = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)
# RFC 8292 §2.4: the example token and the key that signed it (it expired in 2016).
RFC8292_TOKEN = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJhdWQiOiJodHRwczovL3B1c2guZXhhbXBsZS5uZXQiLCJleHAiOjE0NTM1MjM3Njgs"
    "InN1YiI6Im1haWx0bzpwdXNoQGV4YW1wbGUuY29tIn0.i3CYb7t4xfxCDquptFOepC9GAu_HLGkMlMuCGSK2rpiUfnK9ojFwDXb1JrErtmy"
    "sazNjjvW2L9OkSSHzvoD1oA"
)
RFC8292_PUBLIC = "BA1Hxzyi1RUM1b5wjxsn7nGxAszw2u61m164i3MrAIxHF6YK5h4SDYic-dRuU_RCPCfA5aq9ojSwk5Y2EmClBPs"

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"
WITH_USER_NAME = "https://user:pw@fcm.googleapis.com/x"  # URL user info, not email: sensitive-check: allow


def ua_keys() -> tuple[ec.EllipticCurvePrivateKey, Subscription]:
    """A browser's side of a subscription, made up for the test."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return key, Subscription(ENDPOINT, b64url_encode(public), b64url_encode(b"0123456789abcdef"))


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """What a browser does on receipt (RFC 8291 §3.4), written separately from the sender."""
    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    as_public = body[21 : 21 + idlen]
    assert rs == 4096 and idlen == 65
    ua_public = ua_private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = ua_private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public)
    )
    ikm = webpush._hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = webpush._hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = webpush._hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    padded = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


# --- Encryption ---


def test_encryption_matches_the_rfc_8291_example_byte_for_byte():
    as_private = ec.derive_private_key(int.from_bytes(b64url_decode(RFC8291_AS_PRIVATE)), ec.SECP256R1())
    body = webpush.encrypt(
        RFC8291_PLAINTEXT,
        b64url_decode(RFC8291_UA_PUBLIC),
        b64url_decode(RFC8291_AUTH),
        as_private=as_private,
        salt=b64url_decode(RFC8291_SALT),
    )
    assert body == b64url_decode(RFC8291_BODY)
    assert len(body) == 144  # RFC 8291 erratum 5230: the RFC prints 145


def test_the_rfc_8291_example_decrypts_with_the_user_agents_key():
    ua_private = ec.derive_private_key(int.from_bytes(b64url_decode(RFC8291_UA_PRIVATE)), ec.SECP256R1())
    plain = decrypt(b64url_decode(RFC8291_BODY), ua_private, b64url_decode(RFC8291_AUTH))
    assert plain == RFC8291_PLAINTEXT


def test_a_record_matches_the_rfc_8188_example():
    body = webpush.record(
        b64url_decode("yqdlZ-tYemfogSmv7Ws5PQ"), b64url_decode("I1BsxtFttlv3u_Oo94xnmw"), b"I am the walrus"
    )
    assert body == b64url_decode("I1BsxtFttlv3u_Oo94xnmwAAEAAA-NAVub2qFgBEuQKRapoZu-IxkIva3MEB1PD-ly8Thjg")
    assert len(body) == 53  # RFC 8188 erratum 5516: the RFC prints 54


@settings(max_examples=40, deadline=None)
@given(st.binary(max_size=webpush.MAX_PLAINTEXT))
@example(b"\xff" * webpush.MAX_PLAINTEXT)
def test_any_message_up_to_the_limit_round_trips(plaintext):
    ua_private, sub = ua_keys()
    public, secret = sub.keys()
    body = webpush.encrypt(plaintext, public, secret)
    assert len(body) <= 4096
    assert decrypt(body, ua_private, secret) == plaintext


def test_the_largest_message_fills_one_4096_byte_record_exactly():
    ua_private, sub = ua_keys()
    public, secret = sub.keys()
    body = webpush.encrypt(b"x" * 3993, public, secret)
    # RFC 8291 §4: header 86 (salt 16, rs 4, idlen 1, key 65) + message + delimiter 1 + tag 16
    assert len(body) == 4096
    assert decrypt(body, ua_private, secret) == b"x" * 3993


def test_every_message_gets_a_new_salt_and_key():
    _, sub = ua_keys()
    public, secret = sub.keys()
    a, b = webpush.encrypt(b"same", public, secret), webpush.encrypt(b"same", public, secret)
    assert a[:16] != b[:16] and a[21:86] != b[21:86]


def test_a_message_over_the_limit_is_refused():
    _, sub = ua_keys()
    public, secret = sub.keys()
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * (webpush.MAX_PLAINTEXT + 1), public, secret)


@pytest.mark.parametrize(
    ("p256dh", "auth"),
    [
        (RFC8291_UA_PUBLIC[:-4], RFC8291_AUTH),  # too short
        ("B" + "A" * 86, RFC8291_AUTH),  # 65 bytes but not a point on the curve
        (RFC8291_UA_PUBLIC, "BTBZMqHH6r4Tts7J_aSI"),  # secret too short
        ("not base64 !!", RFC8291_AUTH),
    ],
)
def test_bad_subscription_keys_are_refused(p256dh, auth):
    with pytest.raises(webpush.SubscriptionError):  # the device's fault, unlike a settings mistake
        Subscription(ENDPOINT, p256dh, auth).keys()


# --- VAPID ---


def test_the_rfc_8292_example_token_verifies_with_its_key():
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(RFC8292_PUBLIC))
    claims = jwt.decode(
        RFC8292_TOKEN,
        public,
        algorithms=["ES256"],
        audience="https://push.example.net",
        options={"verify_exp": False},
    )
    assert claims["sub"] == "mailto:push@example.com"


def test_a_generated_key_loads_and_gives_a_65_byte_public_key():
    text = webpush.generate_vapid_key()
    assert len(b64url_decode(text)) == 32
    key = webpush.private_key_from_text(text)
    assert len(b64url_decode(webpush.public_key_text(key))) == 65


def test_a_pem_key_loads_too():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    assert webpush.public_key_text(webpush.private_key_from_text(pem)) == webpush.public_key_text(key)


def _pem(key, encryption=None) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption or serialization.NoEncryption(),
    ).decode()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "abc",
        b64url_encode(b"x" * 31),
        _pem(ec.generate_private_key(ec.SECP256R1()), serialization.BestAvailableEncryption(b"passphrase")),
        _pem(ec.generate_private_key(ec.SECP384R1())),  # not P-256
        "-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----",
    ],
)
def test_an_unusable_vapid_key_is_refused_with_value_error(text):
    with pytest.raises(ValueError):  # what turns push off rather than breaking pages
        webpush.private_key_from_text(text)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_the_token_names_the_push_service_and_is_reused_for_an_hour():
    key_text = webpush.generate_vapid_key()
    clock = Clock()
    sender = WebPushSender(key_text, "mailto:it@library.example.edu", clock=clock)
    header = sender.authorization("https://web.push.apple.com/QGuQyavXutnMH")
    token, k = header.removeprefix("vapid t=").split(", k=")
    public = webpush.private_key_from_text(key_text).public_key()
    claims = jwt.decode(
        token,
        public,
        algorithms=["ES256"],
        audience="https://web.push.apple.com",
        options={"verify_exp": False},
    )
    assert claims["sub"] == "mailto:it@library.example.edu"
    assert claims["exp"] == int(clock.now) + 12 * 3600
    assert k == webpush.public_key_text(webpush.private_key_from_text(key_text))

    clock.now += 59 * 60
    assert sender.authorization("https://web.push.apple.com/other") == header  # same service, within the hour
    assert sender.authorization(ENDPOINT) != header  # another service gets its own
    clock.now += 2 * 60
    assert sender.authorization("https://web.push.apple.com/QGuQyavXutnMH") != header  # an hour on: a new one


def test_the_audience_is_the_serialised_origin():
    assert webpush.audience("https://Web.Push.Apple.com:443/x") == "https://web.push.apple.com"
    assert webpush.audience("https://fcm.googleapis.com./fcm/send/x") == "https://fcm.googleapis.com"


def test_the_token_cache_keeps_a_bounded_number_of_push_services():
    sender = WebPushSender(webpush.generate_vapid_key(), "mailto:it@library.example.edu", clock=Clock())
    for i in range(webpush.MAX_TOKENS + 20):
        sender.authorization(f"https://wns2-{i}.notify.windows.com/w/?token=x")
    assert len(sender._tokens) == webpush.MAX_TOKENS


@pytest.mark.parametrize(
    ("subject", "ok"),
    [
        ("mailto:it@library.example.edu", True),
        ("https://stacks.library.example.edu", True),
        ("mailto:stacks@localhost", False),
        ("https://localhost:8000", False),
        ("http://library.example.edu", False),
        ("", False),
    ],
)
def test_vapid_contacts_apple_refuses_are_caught(subject, ok):
    assert webpush.subject_ok(subject) is ok


# --- Which endpoints may be used ---


@pytest.mark.parametrize(
    ("endpoint", "ok"),
    [
        ("https://web.push.apple.com/QGuQyavXutnMH", True),
        ("https://fcm.googleapis.com/fcm/send/abc", True),
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
        ("https://wns2-par02p.notify.windows.com/w/?token=abc", True),
        ("https://fcm.googleapis.com:443/fcm/send/abc", True),
        ("http://fcm.googleapis.com/fcm/send/abc", False),  # not https
        ("https://fcm.googleapis.com:8443/fcm/send/abc", False),  # another port
        (WITH_USER_NAME, False),
        ("https://fcm.googleapis.com.evil.example/x", False),
        ("https://evilpush.apple.com/x", False),  # not a subdomain of push.apple.com
        ("https://internal.library.example.edu/admin", False),
        ("https://10.0.0.5/x", False),
        ("https://[::1]/x", False),
        ("https://fcm.googleapis.com:notaport/x", False),
        ("", False),
        # urllib3 (which requests connects with) ends the host at a backslash; urlsplit doesn't
        ("https://attacker.example\\.fcm.googleapis.com/x", False),
        ("https://10.0.0.5\\.fcm.googleapis.com/x", False),
        ("https://attacker.example\t.fcm.googleapis.com/x", False),
        ("https://fcm.googleapis.com/fcm/send/\u00e9", False),  # non-ASCII
        ("https://fcm.googleapis.com/fcm/send/abc#frag", False),
        ("https://fcm.googleapis.com", False),  # no path
        ("https://a..fcm.googleapis.com/x", False),  # an empty label
        ("https://.fcm.googleapis.com/x", False),
        ("https://" + "a" * 64 + ".fcm.googleapis.com/x", False),  # a label over 63 characters
        (
            "https://fcm.googleapis.com/fcm/send/dXp1:APA91bHun4MxP5egoKMwt2KZFBaFUH-1RYqx",
            True,
        ),  # FCM token with ':'
        ("https://wns2-par02p.notify.windows.com/w/?token=BQYAAAB%2bx", True),
    ],
)
def test_only_known_push_services_are_accepted(endpoint, ok):
    assert webpush.endpoint_allowed(endpoint) is ok


def test_extra_push_hosts_can_be_allowed():
    assert webpush.endpoint_allowed("https://push.example.org/x", ["push.example.org"])
    assert webpush.endpoint_allowed(
        "https://eu.push.example.org/x", ["*.push.example.org"]
    )  # as the guide writes it
    assert not webpush.endpoint_allowed(ENDPOINT, ["push.example.org"])


# --- Sending ---


class Recorder(BaseAdapter):
    """A requests transport that records the request and answers with a set response."""

    def __init__(self, status=201, headers=None, body=b"", error: Exception | None = None):
        super().__init__()
        self.status, self.headers, self.body, self.error = status, headers or {}, body, error
        self.requests: list[requests.PreparedRequest] = []
        self.kwargs: list[dict] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        resp = requests.Response()
        resp.status_code = self.status
        resp.headers.update(self.headers)
        resp._content = self.body
        resp._content_consumed = True
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


def sender_with(recorder: Recorder, **kwargs) -> WebPushSender:
    session = requests.Session()
    session.mount("https://", recorder)
    session.mount("http://", recorder)
    return WebPushSender(
        webpush.generate_vapid_key(), "mailto:it@library.example.edu", session=session, **kwargs
    )


def test_a_push_carries_the_encrypted_message_and_the_rfc_headers():
    rec = Recorder(201)
    ua_private, sub = ua_keys()
    result = sender_with(rec, timeout=7).send(sub, Message(b'{"title":"Hi"}', ttl=259200, urgency="high"))
    assert result == Result(Outcome.SENT, 201)
    (req,) = rec.requests
    assert req.method == "POST" and req.url == ENDPOINT
    assert req.headers["TTL"] == "259200" and req.headers["Urgency"] == "high"
    assert req.headers["Content-Encoding"] == "aes128gcm"
    assert req.headers["Content-Type"] == "application/octet-stream"
    assert req.headers["Authorization"].startswith("vapid t=")
    assert decrypt(req.body, ua_private, sub.keys()[1]) == b'{"title":"Hi"}'
    assert rec.kwargs[0]["timeout"] == 7 and rec.kwargs[0]["stream"] is True


@pytest.mark.parametrize(
    ("status", "headers", "body", "outcome", "retry_after"),
    [
        (201, {}, b"", Outcome.SENT, None),
        (404, {}, b"", Outcome.GONE, None),
        (410, {}, b'{"reason":"Unregistered"}', Outcome.GONE, None),
        (400, {}, b'{"reason":"BadDeviceToken"}', Outcome.REJECTED, None),
        (403, {}, b'{"reason":"BadJwtToken"}', Outcome.REJECTED, None),
        (413, {}, b"", Outcome.REJECTED, None),
        (301, {"Location": "https://internal.example/"}, b"", Outcome.REJECTED, None),  # never followed
        (429, {"Retry-After": "120"}, b"", Outcome.RETRY, 120),
        (503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, b"", Outcome.RETRY, None),
        (500, {}, b"", Outcome.RETRY, None),
    ],
)
def test_push_service_answers_map_to_outcomes(status, headers, body, outcome, retry_after):
    rec = Recorder(status, headers, body)
    _, sub = ua_keys()
    result = sender_with(rec).send(sub, Message(b"{}", ttl=60))
    assert result.outcome is outcome and result.status == status and result.retry_after == retry_after
    assert len(rec.requests) == 1  # a redirect is not followed
    assert ENDPOINT not in result.reason and "abc123" not in result.reason


def test_the_reason_keeps_only_the_push_services_word():
    rec = Recorder(400, body=json.dumps({"reason": "Bad<script>Token https://x"}).encode())
    _, sub = ua_keys()
    assert sender_with(rec).send(sub, Message(b"{}", ttl=60)).reason == "400 BadscriptTokenhttpsx"


def test_an_address_no_connection_can_use_is_the_devices_fault():
    _, sub = ua_keys()
    result = sender_with(Recorder(error=requests.exceptions.InvalidURL())).send(sub, Message(b"{}", ttl=60))
    assert result.outcome is Outcome.REJECTED


@pytest.mark.parametrize("error", [requests.ConnectionError(), requests.Timeout()])
def test_no_answer_means_try_again_later(error):
    _, sub = ua_keys()
    result = sender_with(Recorder(error=error)).send(sub, Message(b"{}", ttl=60))
    assert result.outcome is Outcome.RETRY and result.status is None


def test_an_endpoint_off_the_list_is_never_contacted():
    rec = Recorder(201)
    _, sub = ua_keys()
    sub = Subscription("https://internal.library.example.edu/admin", sub.p256dh, sub.auth)
    assert sender_with(rec).send(sub, Message(b"{}", ttl=60)).outcome is Outcome.REJECTED
    assert rec.requests == []


def test_a_ttl_of_zero_means_now_or_never():  # RFC 8030 §5.2
    rec = Recorder(201)
    _, sub = ua_keys()
    assert sender_with(rec).send(sub, Message(b"{}", ttl=0)).outcome is Outcome.SENT
    assert rec.requests[0].headers["TTL"] == "0"


def test_only_the_start_of_a_refusals_body_is_read():
    body = b'{"pad": "' + b"x" * 100_000 + b'", "reason": "BadJwtToken"}'
    rec = Recorder(403, body=body)
    _, sub = ua_keys()
    assert sender_with(rec).send(sub, Message(b"{}", ttl=60)).reason == "403"  # cut off: not JSON
    rec = Recorder(403, body=b'{"reason": "BadJwtToken"}')
    assert sender_with(rec).send(sub, Message(b"{}", ttl=60)).reason == "403 BadJwtToken"


@pytest.mark.parametrize("message", [Message(b"{}", ttl=-1), Message(b"{}", ttl=60, urgency="urgent")])
def test_ttl_and_urgency_are_checked(message):
    _, sub = ua_keys()
    with pytest.raises(ValueError):
        sender_with(Recorder()).send(sub, message)


def test_the_fake_records_and_answers_as_told():
    _, sub = ua_keys()
    fake = FakePushSender(outcomes={ENDPOINT: Result(Outcome.GONE, 410)})
    assert fake.send(sub, Message(b"{}", ttl=60)).outcome is Outcome.GONE
    assert fake.sent == [(sub, Message(b"{}", ttl=60))]
