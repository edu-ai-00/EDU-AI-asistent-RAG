#!/usr/bin/env python3
# last change in code by Jan Drozd (2026-09-02).
"""Verify FROM OUTSIDE that only what should be exposed is exposed.

    python3 scripts/perimeter_check.py --ip <public IPv4 of the instance>

Must run somewhere that is NOT on the VPN — this is the one check the production
server cannot perform on itself, because from the inside every door looks open.
Check 0 proves the vantage point before anything else is believed.

Why it exists: the /search/ API key was removed, so nothing inside nginx
authenticates a caller any more. Two things now hold the perimeter, both of them
configured outside this repository, neither diffable in a pull request:

  * Cloudflare Access on rag.edu-ai.eu  -> the tunnel (a Zero Trust policy)
  * the AWS security group              -> :80, :8443, :8444 on the public IP

Both are one dashboard click away from being wide open, and nothing in the stack
would notice. This is what notices. Run it on a schedule, from off-site.

Checks:

  0. vantage point   — our egress IP differs from the target's, so a "closed"
                       result means closed from the internet and not merely
                       "routed down the WireGuard tunnel and dropped"
  1. tunnel guarded  — an unauthenticated request to the tunnel hostname is
                       refused, AND Cloudflare Access is what refuses it
  2. ports shut      — :80, :8443, :8444, :22, :8090 do not answer on the
                       public IP
  3. last resort     — runs ONLY if check 1 found Access open: whether nginx's
                       path allowlist still keeps the admin endpoints off the
                       tunnel. A pass here is not reassurance, it is the final
                       layer reporting that it is now the only one.

A check that could not run is reported [skip], never [pass]: a green run must
never mean nothing was tested.

Exit code 0 = everything that ran passed. 2 = the run itself was not valid.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):   # cp1250 console would die on a stray glyph
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_HOST = "rag.edu-ai.eu"

# Cloudflare refuses python-urllib's default UA as a bot BEFORE Access is
# consulted — a 403 with no Cf-Access-* header, which reads exactly like "the
# policy is gone". Measured 2026-09-02. Send something it will pass through so
# the check tests Access and not the bot rules.
USER_AGENT = "curl/8.4.0 (edu_ai perimeter_check)"

# Doors that must never answer from the internet. The SG is the only thing
# holding them: nginx publishes 80/8443/8444 on 0.0.0.0 by deliberate choice.
GUARDED_PORTS = [
    (80, "nginx — /search/, /api/, /chat/"),
    (8443, "pgAdmin"),
    (8444, "Langflow UI"),
    (22, "sshd"),
    (8090, "bifrost — provider keys, no auth of its own"),
]

CONNECT_TIMEOUT = 6
HTTP_TIMEOUT = 15

_passed = 0
_failed = 0
_skipped = 0


def ok(message: str) -> None:
    global _passed
    _passed += 1
    print(f"  [pass] {message}")


def bad(message: str) -> None:
    global _failed
    _failed += 1
    print(f"  [FAIL] {message}")


def skip(message: str) -> None:
    global _skipped
    _skipped += 1
    print(f"  [skip] {message}")


def egress_ip() -> str | None:
    """Our own address as the internet sees it."""
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                return response.read().decode().strip()
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return None


def check_vantage_point(target_ip: str | None) -> bool:
    """True if a negative result from here can be trusted.

    On a WireGuard peer, traffic to the instance's public IP normally leaves via
    the default route — but if someone widens AllowedIPs to 0.0.0.0/0 it goes
    down the tunnel instead, and every port then looks shut for the wrong reason.
    """
    print("\n0. vantage point")

    mine = egress_ip()
    if mine is None:
        skip("could not determine our egress IP — no verdict is trustworthy")
        return False
    print(f"         our egress IP: {mine}")

    if target_ip and mine == target_ip:
        bad(f"we are behind the same address as the target ({mine}) — this run "
            "is inside the perimeter and proves nothing. Run it off-site.")
        return False

    ok("running from outside the target's address")
    return True


def _head(url: str) -> tuple[int, dict[str, str], bytes] | None:
    """GET without following redirects — a 302 to a login page is a real answer."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, dict(response.headers), response.read(2048)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(2048)
    except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError):
        return None


def check_tunnel_guarded(host: str) -> bool:
    """Returns True if Cloudflare Access is the thing refusing us."""
    print("\n1. tunnel guarded by Cloudflare Access")

    result = _head(f"https://{host}/search/health")
    if result is None:
        skip(f"https://{host} did not respond — cannot tell a closed door from "
             "an outage, and an outage is not a pass")
        return False

    status, headers, body = result
    access_headers = {k: v for k, v in headers.items() if k.lower().startswith("cf-access")}

    if access_headers:
        if status == 200:
            bad(f"Access answered 200 — the policy admits an anonymous caller. "
                f"/search/ is open to the internet. ({sorted(access_headers)})")
            return False
        ok(f"HTTP {status} from Cloudflare Access "
           f"({', '.join(sorted(access_headers))})")
        return True

    # No Access header. Two very different worlds, and calling them both a
    # failure is how an alert gets ignored: Cloudflare may have refused us
    # itself (bot rules, WAF) without ever consulting Access, or the request
    # reached our origin and Access is genuinely out of the path.
    served_by = headers.get("Server", "?")
    looks_like_ours = b'"error"' in body or b'"status"' in body

    if looks_like_ours or status == 200:
        bad(f"HTTP {status} and our own origin answered (Server: {served_by}"
            f"{', our JSON envelope' if looks_like_ours else ''}) -- the Access "
            "policy is NOT in the path. nginx is answering the internet "
            "directly and nothing inside it authenticates a caller.")
        return False

    if "cloudflare" in served_by.lower():
        skip(f"HTTP {status} from Cloudflare but with no Cf-Access-* header -- "
             "something refused us before Access was consulted (bot rules or "
             "WAF). The origin was not reached, so nothing is exposed, but "
             "Access itself was NOT verified. Check the User-Agent.")
        return True

    skip(f"HTTP {status} from an unexpected Server: {served_by} -- cannot tell "
         "what answered, so this is not a pass.")
    return True


def check_ports_shut(ip: str) -> None:
    print(f"\n2. ports shut on {ip}")

    for port, what in GUARDED_PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect((ip, port))
        except socket.timeout:
            ok(f":{port:<5} dropped (filtered) — {what}")
        except ConnectionRefusedError:
            # Reachable but nothing listening: still shut, but the SG is letting
            # the packet through, so a service started here would be exposed.
            ok(f":{port:<5} refused — nothing listening, but the SG admits the "
               f"packet — {what}")
        except OSError as exc:
            skip(f":{port:<5} inconclusive ({exc.__class__.__name__}) — {what}")
        else:
            bad(f":{port:<5} ANSWERED from the internet — {what}")
        finally:
            sock.close()


def check_allowlist_last_resort(host: str) -> None:
    """Only meaningful once Access is already open — see the module docstring."""
    print("\n3. nginx path allowlist (last line of defence)")

    for path in ("/search/v1/query-log", "/search/v1/staff-users"):
        result = _head(f"https://{host}{path}")
        if result is None:
            skip(f"{path} did not respond")
            continue
        status, _, _ = result
        if status == 404:
            ok(f"{path} -> 404, still off the tunnel")
        else:
            bad(f"{path} -> HTTP {status} — reachable from the internet. This is "
                "personal data (questions asked, staff lists).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("EDU_AI_TUNNEL_HOST", DEFAULT_HOST),
                        help=f"tunnel hostname (default: {DEFAULT_HOST})")
    parser.add_argument("--ip", default=os.environ.get("EDU_AI_PUBLIC_IP"),
                        help="public IPv4 of the instance; without it check 2 is skipped")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    # Use this whenever nothing is reading the output. Unattended, a skip is
    # indistinguishable from a pass, and a canary that cannot reach anything
    # would report green forever.
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on skips too — REQUIRED for scheduled runs")
    args = parser.parse_args()

    print("edu_ai perimeter check — run this from OFF-SITE")

    valid = check_vantage_point(args.ip)

    if not valid:
        print("\nThe vantage point could not be established, so no result below "
              "would mean what it says. Nothing else was run.")
        if args.json:
            print(json.dumps({"valid": False, "passed": _passed, "failed": _failed,
                              "skipped": _skipped}))
        return 2

    access_holding = check_tunnel_guarded(args.host)

    if args.ip:
        check_ports_shut(args.ip)
    else:
        skip("no --ip / EDU_AI_PUBLIC_IP given — the security group was NOT checked")

    if not access_holding:
        check_allowlist_last_resort(args.host)

    print(f"\n{_passed} passed, {_failed} failed, {_skipped} skipped")
    if _skipped:
        print("Skipped checks were not run — they are not passes."
              + ("" if args.strict else "  (--strict would exit non-zero here)"))
    if args.json:
        print(json.dumps({"valid": True, "passed": _passed, "failed": _failed,
                          "skipped": _skipped, "strict": args.strict}))
    if _failed:
        return 1
    return 1 if (args.strict and _skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
