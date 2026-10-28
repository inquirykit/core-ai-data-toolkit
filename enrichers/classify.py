"""
Which entity type a value is, and the value normalised for lookups.

Defanged forms are read ("1.2.3[.]4", "hxxp://"), because that is how
indicators are shared. A value that fits nothing is ``generic``; the caller
can also name the type itself to skip the guess.
"""

import ipaddress
import re
from typing import Tuple
from urllib.parse import urlsplit

from .spec import EntityType

_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.I)
_ASN = re.compile(r"^AS\d{1,10}$", re.I)
_EMAIL = re.compile(r"^[^@\s]+@([a-z0-9-]+\.)+[a-z]{2,}$", re.I)
_HEX = re.compile(r"^[0-9a-f]+$", re.I)
_HASH_TYPES = {32: EntityType.MD5, 40: EntityType.SHA1, 64: EntityType.SHA256}
_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)
_PHONE = re.compile(r"^\+?[0-9]{7,15}$")
_BTC = re.compile(r"^([13][a-km-zA-HJ-NP-Z1-9]{25,34}|bc1[a-z0-9]{25,62})$")
_ETH = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRON = re.compile(r"^T[a-km-zA-HJ-NP-Z1-9]{33}$")


def refang(value: str) -> str:
    """'1.2.3[.]4', 'hxxps://x[.]y' -> the real form."""
    value = value.strip()
    value = re.sub(r"\[\.\]|\(\.\)|\{\.\}", ".", value)
    value = re.sub(r"^hxxp(s?)://", r"http\1://", value, flags=re.I)
    value = re.sub(r"\[@\]|\(@\)", "@", value)
    return value


def classify(value: str) -> Tuple[EntityType, str]:
    """(type, normalised value) for ``value``."""
    v = refang(value)
    if not v:
        return EntityType.GENERIC, v
    try:
        ip = ipaddress.ip_address(v)
        return (EntityType.IPV4 if ip.version == 4 else EntityType.IPV6), str(ip)
    except ValueError:
        pass
    if "/" in v and "://" not in v:
        try:
            return EntityType.CIDR, str(ipaddress.ip_network(v, strict=False))
        except ValueError:
            pass
    if _CVE.match(v):
        return EntityType.CVE, v.upper()
    if _ASN.match(v):
        return EntityType.ASN, v.upper()
    if _EMAIL.match(v):
        return EntityType.EMAIL, v.lower()
    if "://" in v:
        parts = urlsplit(v)
        if parts.scheme in ("http", "https", "ftp") and parts.netloc:
            return EntityType.URL, v
    if _HEX.match(v) and len(v) in _HASH_TYPES:
        return _HASH_TYPES[len(v)], v.lower()
    if _ETH.match(v) or _BTC.match(v) or _TRON.match(v):
        return EntityType.CRYPTO_WALLET, v
    digits = re.sub(r"[\s().-]", "", v)
    if _PHONE.match(digits) and (v.startswith("+") or len(digits) >= 10):
        return EntityType.PHONE, "+" + digits.lstrip("+") if v.startswith("+") else digits
    if _DOMAIN.match(v.rstrip(".")):
        return EntityType.DOMAIN, v.lower().rstrip(".")
    return EntityType.GENERIC, v


def normalise(value: str, type_: EntityType) -> str:
    """The value as lookups want it when the caller named the type."""
    v = refang(value)
    if type_ in (EntityType.IPV4, EntityType.IPV6):
        try:
            return str(ipaddress.ip_address(v))
        except ValueError:
            return v
    if type_ in (EntityType.DOMAIN, EntityType.EMAIL, EntityType.MD5, EntityType.SHA1, EntityType.SHA256):
        return v.lower().rstrip(".") if type_ == EntityType.DOMAIN else v.lower()
    if type_ in (EntityType.CVE, EntityType.ASN):
        return v.upper()
    return v
