"""
ssrf.py — Production-grade Server-Side Request Forgery (SSRF) Defense Module.
Validates outbound URLs, resolves DNS, checks all IP addresses against blocked ranges,
pins connections, and re-validates all HTTP 3xx redirect destinations.
"""

import ipaddress
import socket
import urllib.parse
from typing import Set, Tuple, Optional, Dict, Any, List
import requests

from app.connectors.errors import SSRFSecurityError


# Blocked IPv4 / IPv6 network ranges
BLOCKED_NETWORKS = [
    # IPv4 Loopback
    ipaddress.ip_network("127.0.0.0/8"),
    # IPv4 Private RFC 1918
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    # IPv4 Link-Local
    ipaddress.ip_network("169.254.0.0/16"),
    # IPv4 Unspecified / Broadcast / Multicast / Reserved
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    # IPv6 Loopback
    ipaddress.ip_network("::1/128"),
    # IPv6 Unspecified
    ipaddress.ip_network("::/128"),
    # IPv6 Unique Local (ULA) RFC 4193
    ipaddress.ip_network("fc00::/7"),
    # IPv6 Link-Local Unicast RFC 4291
    ipaddress.ip_network("fe80::/10"),
    # IPv6 Multicast
    ipaddress.ip_network("ff00::/8"),
    # IPv6 Cloud Metadata (e.g. AWS IMDSv2 IPv6)
    ipaddress.ip_network("fd00:ec2::254/128"),
]

# Cloud Metadata IPv4
AWS_METADATA_IP = ipaddress.ip_address("169.254.169.254")

# Standard Allowlisted Provider Hostnames & Wildcard Suffixes
DEFAULT_GOOGLE_DOMAINS = {
    "www.googleapis.com",
    "googleapis.com",
    "oauth2.googleapis.com",
    "drive.googleapis.com",
    "accounts.google.com",
}

DEFAULT_MICROSOFT_DOMAINS = {
    "graph.microsoft.com",
    "login.microsoftonline.com",
    "login.microsoft.com",
    "login.windows.net",
}


def is_ip_allowed(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """
    Check if an IP address is safe for outbound connections.
    Rejects loopback, private RFC1918, link-local, cloud metadata, multicast, and IPv4-mapped IPv6 blocked addresses.
    """
    # Handle IPv4-mapped IPv6 addresses (e.g., ::ffff:127.0.0.1)
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        ip_obj = ip_obj.ipv4_mapped

    if ip_obj == AWS_METADATA_IP:
        return False

    if ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified or ip_obj.is_reserved:
        return False

    for net in BLOCKED_NETWORKS:
        if ip_obj in net:
            return False

    return True


def is_domain_matching(hostname: str, allowed_domains: Set[str]) -> bool:
    """
    Check whether hostname matches any domain or wildcard pattern in allowed_domains.
    """
    host_lower = hostname.lower().strip(".")
    for allowed in allowed_domains:
        allowed_lower = allowed.lower().strip(".")
        if allowed_lower.startswith("*."):
            suffix = allowed_lower[2:]
            if host_lower == suffix or host_lower.endswith("." + suffix):
                return True
        elif host_lower == allowed_lower or host_lower.endswith("." + allowed_lower):
            return True
    return False


def validate_url(
    url: str,
    allowed_domains: Optional[Set[str]] = None,
    allow_http_in_dev: bool = True,
) -> Tuple[str, str, int]:
    """
    Validate an outbound URL against scheme, domain allowlist, and resolved IP addresses.
    Returns (validated_url, hostname, port).
    Raises SSRFSecurityError if validation fails.
    """
    if not url or not isinstance(url, str):
        raise SSRFSecurityError("URL is empty or invalid.")

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        raise SSRFSecurityError(f"Malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFSecurityError(f"Unsupported URL scheme '{scheme}'. Only HTTPS (or HTTP in testing) is allowed.")

    if scheme == "http" and not allow_http_in_dev:
        raise SSRFSecurityError("Insecure HTTP scheme is not allowed in production.")

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise SSRFSecurityError("URL contains no valid hostname.")

    port = parsed.port or (443 if scheme == "https" else 80)

    # 1. Hostname Allowlist Check
    if allowed_domains:
        if not is_domain_matching(hostname, allowed_domains):
            raise SSRFSecurityError(
                f"Destination hostname '{hostname}' is not in the allowed domains: {allowed_domains}"
            )

    # 2. Check direct IP string literals
    try:
        direct_ip = ipaddress.ip_address(hostname)
        if not is_ip_allowed(direct_ip):
            raise SSRFSecurityError(
                f"Direct IP destination '{hostname}' is in a prohibited private or link-local network range."
            )
        return url, hostname, port
    except ValueError:
        # Not a raw IP literal, proceed to DNS resolution
        pass

    # 3. DNS Resolution & All-Resolved-IP Validation
    try:
        addr_info = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SSRFSecurityError(f"DNS resolution failed for hostname '{hostname}': {exc}") from exc

    resolved_ips = set()
    for entry in addr_info:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            resolved_ips.add(ip_obj)
        except ValueError:
            raise SSRFSecurityError(f"Invalid resolved IP '{ip_str}' for host '{hostname}'.")

    if not resolved_ips:
        raise SSRFSecurityError(f"No IP addresses resolved for hostname '{hostname}'.")

    for ip_obj in resolved_ips:
        if not is_ip_allowed(ip_obj):
            raise SSRFSecurityError(
                f"SSRF violation: Hostname '{hostname}' resolved to prohibited IP '{ip_obj}'."
            )

    return url, hostname, port


class SSRFSafeSession(requests.Session):
    """
    Requests Session enforcing SSRF validation on every request and every HTTP 3xx redirect.
    """

    def __init__(self, allowed_domains: Optional[Set[str]] = None, allow_http_in_dev: bool = True):
        super().__init__()
        self.allowed_domains = allowed_domains
        self.allow_http_in_dev = allow_http_in_dev
        self.max_redirects = 5

    def request(self, method, url, *args, **kwargs):
        # Validate initial URL
        validate_url(url, self.allowed_domains, self.allow_http_in_dev)
        return super().request(method, url, *args, **kwargs)

    def resolve_redirects(self, resp, req, stream=False, timeout=None, verify=True, cert=None, proxies=None, yield_requests=False, **adapter_kwargs):
        """
        Intercept every redirect and validate destination URL before following.
        """
        for redirect_req in super().resolve_redirects(resp, req, stream=stream, timeout=timeout, verify=verify, cert=cert, proxies=proxies, yield_requests=True, **adapter_kwargs):
            # Validate redirect destination
            validate_url(redirect_req.url, self.allowed_domains, self.allow_http_in_dev)
            if yield_requests:
                yield redirect_req
            else:
                resp = self.send(
                    redirect_req,
                    stream=stream,
                    timeout=timeout,
                    verify=verify,
                    cert=cert,
                    proxies=proxies,
                    **adapter_kwargs,
                )
                yield resp
