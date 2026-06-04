"""Atheris fuzz harness for ``provisioning.validate_clone_url`` — the SSRF guard.

``validate_clone_url`` is the security gate on user-supplied clone URLs: it
allowlists the scheme and blocks URLs resolving to private/loopback/link-local
addresses. It legitimately *raises* ``InvalidCloneUrl``/``BlockedCloneHost`` to
reject a URL, so those are caught here; anything else bubbling up is a real bug
(a parser crash, or an SSRF-relevant edge case).

DNS is monkeypatched to a fuzzer-chosen address so the harness stays deterministic
and offline while still exercising the URL parse + IP-classification path (the
real ``socket.getaddrinfo`` would do live, slow, non-reproducible lookups).
"""

import socket
import sys

import atheris

with atheris.instrument_imports():
    from clauster import provisioning
    from clauster.config import CloneConfig

_CFG = CloneConfig()
# A mix of public + blocked (loopback/private/link-local/IPv6) addresses so the
# fuzzer drives both the allow and the SSRF-reject branches.
_CANDIDATE_IPS = (
    "93.184.216.34",
    "8.8.8.8",
    "127.0.0.1",
    "10.0.0.1",
    "192.168.1.5",
    "169.254.169.254",
    "::1",
    "fd00::1",
)


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    ip = _CANDIDATE_IPS[fdp.ConsumeIntInRange(0, len(_CANDIDATE_IPS) - 1)]
    url = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]

    original = provisioning.socket.getaddrinfo
    provisioning.socket.getaddrinfo = _fake_getaddrinfo
    try:
        provisioning.validate_clone_url(url, _CFG)
    except provisioning.ProvisionError:
        pass  # expected rejection (bad scheme / no host / blocked address)
    finally:
        provisioning.socket.getaddrinfo = original


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
