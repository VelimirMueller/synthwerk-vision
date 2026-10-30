"""Strict parser for a browser `Origin` value (infra#24).

It mirrors `CanonicalOrigin` of synthwerk-identity (`internal/apps/origin.go`) and of
synthwerk-llm (`internal/cors/origin.go`). Vision asks identity only about origins that pass
it, so a caller cannot fill the cache with values identity would refuse anyway. Keep the
parsers the same.
"""

import ipaddress

MAX_ORIGIN_LEN = 256  # contracts AppOrigin
_MAX_HOST_LEN = 253
_MAX_LABEL_LEN = 63
_DEFAULT_PORT = {"https": "443", "http": "80"}
# Loopback hosts may use http. Every other host needs https.
_LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class OriginError(ValueError):
    """The value is not an origin vision accepts."""


def canonical_origin(value: str) -> str:
    """Check `value` and return its canonical form: scheme://host[:port], lower case, default
    port removed, IP addresses in their short form.

    The parser is strict on purpose. It refuses what a browser never sends in an Origin
    header: "null", a path (even "/"), a query, a fragment, user info, a wildcard,
    percent-encoding, a backslash, white space, non-ASCII bytes, a trailing dot and a port
    with leading zeros. http is accepted only for localhost and loopback IPs.
    """
    if not value or len(value) > MAX_ORIGIN_LEN or value == "null":
        raise OriginError
    # ASCII only, before any case folding: lower() maps some non-ASCII characters to ASCII.
    if any(c <= " " or c >= "\x7f" for c in value):
        raise OriginError
    scheme, sep, rest = value.partition("://")
    if not sep:
        raise OriginError
    scheme = scheme.lower()
    if scheme not in _DEFAULT_PORT:
        raise OriginError
    if not rest or any(c in rest for c in "/?#@\\%*"):
        raise OriginError
    raw_host, port = _split_host_port(rest)
    host = _canonical_host(raw_host)
    if scheme == "http" and host not in _LOOPBACK:
        raise OriginError
    if port == _DEFAULT_PORT[scheme]:
        port = ""
    return f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"


def _split_host_port(value: str) -> tuple[str, str]:
    """Split "host", "host:port", "[v6]" and "[v6]:port". The port is 1 to 65535, digits
    only, no leading zero."""
    port: str | None = None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise OriginError
        host, after = value[: end + 1], value[end + 1 :]
        if after:
            if after[0] != ":":
                raise OriginError
            port = after[1:]
    else:
        if value.count(":") > 1:
            raise OriginError
        host, sep, tail = value.partition(":")
        if sep:
            port = tail
    if port is not None and (
        not port.isdigit() or not port.isascii() or port[0] == "0" or int(port) > 65535
    ):
        raise OriginError
    return host, port or ""


def _canonical_host(host: str) -> str:
    """Check a DNS name, an IPv4 address or a bracketed IPv6 address; return it in lower case
    and short form."""
    if not host or len(host) > _MAX_HOST_LEN:
        raise OriginError
    if host.startswith("["):
        # _split_host_port ends it with "]". A zone ("%eth0") never gets here: "%" is refused.
        try:
            return f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
        except ValueError:
            raise OriginError from None
    host = host.lower()
    if not host.strip("0123456789."):
        # All digits and dots: an IPv4 address. ipaddress refuses leading zeros, so
        # "010.0.0.1" never means an octal address.
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            raise OriginError from None
    for label in host.split("."):
        if (
            not label
            or len(label) > _MAX_LABEL_LEN
            or label[0] == "-"
            or label[-1] == "-"
            or not set(label) <= _LABEL_CHARS
        ):
            raise OriginError
    return host
