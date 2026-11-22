"""A structurally valid RFC 3161 response with a placeholder signature, for plumbing tests.

``rfc3161_client.decode_timestamp_response`` parses the ASN.1 and exposes the status, the message
imprint and the generation time, which is everything the manifest code reads before it verifies a
signature chain. The signature bytes here are not a real signature: nothing in the suite claims to
verify one (that needs a real TSA; ``efp selftest --live`` and ``efp manifest --verify`` do it).
"""

from __future__ import annotations

from datetime import UTC, datetime


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def tlv(tag: int, *parts: bytes) -> bytes:
    body = b"".join(parts)
    return bytes([tag]) + _len(len(body)) + body


def integer(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)
    return tlv(0x02, raw)


def oid(dotted: str) -> bytes:
    nums = [int(x) for x in dotted.split(".")]
    out = bytes([nums[0] * 40 + nums[1]])
    for n in nums[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        out += bytes(reversed(chunk))
    return tlv(0x06, out)


SHA256 = "2.16.840.1.101.3.4.2.1"
SIGNED_DATA = "1.2.840.113549.1.7.2"
TST_INFO = "1.2.840.113549.1.9.16.1.4"
RSA_SHA256 = "1.2.840.113549.1.1.11"


def alg(dotted: str) -> bytes:
    return tlv(0x30, oid(dotted), tlv(0x05))


def make_tsr(imprint_sha256: bytes, when: datetime | None = None, *, granted: bool = True) -> bytes:
    when = (when or datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)).astimezone(UTC)
    status = tlv(0x30, integer(0 if granted else 2))
    if not granted:
        return tlv(0x30, status)
    tst_info = tlv(
        0x30,
        integer(1),
        oid("1.2.3.4.1"),
        tlv(0x30, alg(SHA256), tlv(0x04, imprint_sha256)),
        integer(4242),
        tlv(0x18, when.strftime("%Y%m%d%H%M%SZ").encode()),
    )
    encap = tlv(0x30, oid(TST_INFO), tlv(0xA0, tlv(0x04, tst_info)))
    signer = tlv(
        0x30,
        integer(1),
        tlv(0x30, tlv(0x30), integer(7)),  # issuerAndSerialNumber with an empty issuer name
        alg(SHA256),
        alg(RSA_SHA256),
        tlv(0x04, b"\x00" * 32),  # a placeholder, not a signature
    )
    signed_data = tlv(0x30, integer(3), tlv(0x31, alg(SHA256)), encap, tlv(0x31, signer))
    content_info = tlv(0x30, oid(SIGNED_DATA), tlv(0xA0, signed_data))
    return tlv(0x30, status, content_info)
