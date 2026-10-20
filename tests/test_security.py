"""Tests for request hardening.

These are the hostile probes from the charter: wrong Host, cross-origin
mutation, missing or mismatched CSRF, and proxy/forwarded headers that would
weaken canonical-origin handling.
"""

from __future__ import annotations

import unittest

from gateway.http.core import Headers, Request
from gateway.http.security import (
    SecurityError,
    SecurityPolicy,
    build_allowed_hosts,
    build_canonical_origins,
    check_csrf,
    check_forwarded_headers,
    check_host,
    check_origin,
    is_loopback_client,
    require_loopback,
)


def make_request(
    method: str = "GET",
    path: str = "/owner/",
    headers: list[tuple[str, str]] | None = None,
    client: str = "10.10.11.50",
) -> Request:
    items = list(headers or [])
    if not any(key.lower() == "host" for key, _ in items):
        items.append(("Host", "10.10.11.109:8765"))
    return Request(
        method=method,
        path=path,
        raw_query="",
        headers=Headers(items),
        body=b"",
        client_address=(client, 40000),
        server_port=8765,
    )


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = SecurityPolicy(
            allowed_hosts=build_allowed_hosts("10.10.11.109", 8765),
            canonical_origins=build_canonical_origins("10.10.11.109", 8765),
        )


class ForwardedHeaderTests(PolicyTests):
    def test_forwarded_header_is_rejected_not_ignored(self) -> None:
        request = make_request(headers=[("X-Forwarded-For", "203.0.113.9")])
        with self.assertRaises(SecurityError) as ctx:
            check_forwarded_headers(request)
        self.assertEqual(ctx.exception.status, 400)

    def test_every_forwarded_header_family_is_rejected(self) -> None:
        for header in (
            "Forwarded",
            "X-Forwarded-Host",
            "X-Forwarded-Proto",
            "X-Forwarded-Port",
            "X-Real-IP",
            "X-Original-URL",
            "True-Client-IP",
            "CF-Connecting-IP",
        ):
            with self.subTest(header=header):
                request = make_request(headers=[(header, "value")])
                with self.assertRaises(SecurityError):
                    check_forwarded_headers(request)

    def test_ordinary_headers_pass(self) -> None:
        request = make_request(headers=[("Accept", "text/html"), ("User-Agent", "curl/8")])
        check_forwarded_headers(request)


class HostTests(PolicyTests):
    def test_matching_host_is_accepted(self) -> None:
        check_host(make_request(), self.policy)

    def test_mismatched_host_is_rejected(self) -> None:
        request = make_request(headers=[("Host", "evil.example.com")])
        with self.assertRaises(SecurityError) as ctx:
            check_host(request, self.policy)
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_host_is_rejected(self) -> None:
        request = Request("GET", "/owner/", "", Headers([]), b"", ("10.0.0.1", 1), 8765)
        with self.assertRaises(SecurityError):
            check_host(request, self.policy)

    def test_allowed_hosts_contain_bare_and_port_forms(self) -> None:
        hosts = build_allowed_hosts("10.10.11.109", 8765)
        self.assertIn("10.10.11.109:8765", hosts)
        self.assertIn("10.10.11.109", hosts)


class OriginTests(PolicyTests):
    def test_matching_origin_is_accepted(self) -> None:
        request = make_request(method="POST", headers=[("Origin", "http://10.10.11.109:8765")])
        check_origin(request, self.policy)

    def test_cross_origin_mutation_is_rejected(self) -> None:
        request = make_request(method="POST", headers=[("Origin", "http://evil.example.com")])
        with self.assertRaises(SecurityError) as ctx:
            check_origin(request, self.policy)
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_origin_on_mutation_is_rejected(self) -> None:
        request = make_request(method="POST")
        with self.assertRaises(SecurityError):
            check_origin(request, self.policy)

    def test_get_does_not_require_an_origin(self) -> None:
        check_origin(make_request(method="GET"), self.policy)

    def test_api_surface_mutation_does_not_require_an_origin(self) -> None:
        # The API authenticates with bearer tokens, which a browser never
        # sends ambiently, so Origin/CSRF are not the right controls there.
        request = make_request(method="POST", path="/v1/chat/completions")
        check_origin(request, self.policy)


class CsrfTests(PolicyTests):
    def test_matching_token_is_accepted(self) -> None:
        request = make_request(method="POST", headers=[("X-Gateway-CSRF", "tok")])
        check_csrf(request, "tok")

    def test_missing_token_is_rejected(self) -> None:
        request = make_request(method="POST")
        with self.assertRaises(SecurityError) as ctx:
            check_csrf(request, "tok")
        self.assertEqual(ctx.exception.status, 403)

    def test_mismatched_token_is_rejected(self) -> None:
        request = make_request(method="POST", headers=[("X-Gateway-CSRF", "other")])
        with self.assertRaises(SecurityError):
            check_csrf(request, "tok")

    def test_get_does_not_require_a_token(self) -> None:
        check_csrf(make_request(method="GET"), "tok")


class LoopbackTests(PolicyTests):
    def test_setup_surface_is_refused_from_a_lan_client(self) -> None:
        request = make_request(path="/setup/providers", client="10.10.11.50")
        with self.assertRaises(SecurityError) as ctx:
            require_loopback(request, self.policy)
        self.assertEqual(ctx.exception.status, 403)

    def test_setup_surface_is_allowed_from_loopback(self) -> None:
        request = make_request(path="/setup/providers", client="127.0.0.1")
        require_loopback(request, self.policy)

    def test_owner_surface_is_not_loopback_only(self) -> None:
        request = make_request(path="/owner/", client="10.10.11.50")
        require_loopback(request, self.policy)

    def test_loopback_detection(self) -> None:
        self.assertTrue(is_loopback_client("127.0.0.1"))
        self.assertTrue(is_loopback_client("::1"))
        self.assertFalse(is_loopback_client("10.10.11.50"))
        self.assertFalse(is_loopback_client("not-an-ip"))


if __name__ == "__main__":
    unittest.main()
