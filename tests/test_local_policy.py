"""The destination policy for owner-configured local endpoints.

Covers test-matrix cases 1-5 of the T15 spec:

1. private and loopback literals, explicit ports and a ``/v1`` prefix are
   accepted and normalised;
2. public, link-local/metadata, unspecified, multicast, credential-bearing,
   query/fragment, bad-port, wildcard and non-HTTP URLs are refused;
3. a hostname is judged on *all* of its answers, with a stub resolver;
4. a connect-time peer outside the private ranges is refused **before a
   single request byte is written** (DNS rebinding);
5. a redirect from a local endpoint is refused and never replayed.

No network access beyond loopback, no credentials, no provider calls.
"""

from __future__ import annotations

import socket
import unittest

from gateway.adapters.base import AdapterError
from gateway.adapters.http_client import UpstreamClient
from gateway.adapters.local_policy import (
    REASON_CREDENTIAL_IN_URL,
    REASON_HOST_MISSING,
    REASON_PATH,
    REASON_PORT,
    REASON_PRIVATE,
    REASON_RESOLVE,
    REASON_SCHEME,
    REASON_UNRESOLVED,
    LocalEndpointError,
    is_private_address,
    make_peer_check,
    normalize_base_url,
)
from gateway.config import ERROR_POLICY
from tests.local_fixture import LocalFixture


def resolver_returning(*addresses):
    return lambda host, port: list(addresses)


class AcceptedEndpointTests(unittest.TestCase):
    def test_private_and_loopback_literals_are_accepted(self) -> None:
        cases = {
            "http://127.0.0.1": "http://127.0.0.1",
            "http://127.0.0.1:11434": "http://127.0.0.1:11434",
            "http://10.1.2.3:8080": "http://10.1.2.3:8080",
            "http://172.16.0.9": "http://172.16.0.9",
            "http://172.31.255.1": "http://172.31.255.1",
            "http://192.168.1.10:1234": "http://192.168.1.10:1234",
            "http://[fd00::1]:8443": "http://[fd00::1]:8443",
            "http://127.0.0.1:11434/v1": "http://127.0.0.1:11434",
            "http://127.0.0.1:11434/v1/": "http://127.0.0.1:11434",
            "https://192.168.0.2": "https://192.168.0.2",
            "HTTP://127.0.0.1:11434": "http://127.0.0.1:11434",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_base_url(raw), expected)

    def test_a_hostname_that_resolves_only_private_is_accepted(self) -> None:
        url = normalize_base_url(
            "http://ollama.lan:11434/v1", resolver=resolver_returning("192.168.1.50")
        )
        self.assertEqual(url, "http://ollama.lan:11434")

    def test_a_path_prefix_is_kept(self) -> None:
        url = normalize_base_url(
            "http://192.168.1.5/gateway", resolver=resolver_returning("192.168.1.5")
        )
        self.assertEqual(url, "http://192.168.1.5/gateway")

    def test_default_ports_are_omitted_and_explicit_ones_kept(self) -> None:
        self.assertEqual(normalize_base_url("https://10.0.0.2:443"), "https://10.0.0.2")
        self.assertEqual(normalize_base_url("https://10.0.0.2:8443"), "https://10.0.0.2:8443")


class RefusedEndpointTests(unittest.TestCase):
    def assert_refused(self, raw: str, reason: str, resolver=None) -> None:
        with self.assertRaises(LocalEndpointError) as caught:
            normalize_base_url(raw, resolver=resolver)
        self.assertEqual(caught.exception.reason, reason)

    def test_public_metadata_link_local_and_unspecified_are_refused(self) -> None:
        for raw in (
            "http://8.8.8.8",
            "http://93.184.216.34:8080",
            "http://169.254.169.254",          # cloud metadata
            "http://[fe80::1]:8080",           # link-local IPv6
            "http://0.0.0.0:8080",
            "http://[::]:8080",
            "http://224.0.0.1:8080",           # multicast
            "http://255.255.255.255",          # broadcast
            "http://[::ffff:169.254.169.254]", # metadata behind an IPv4-mapped literal
        ):
            with self.subTest(raw=raw):
                self.assert_refused(raw, REASON_PRIVATE)

    def test_credentials_query_and_fragment_are_refused(self) -> None:
        marker = "supersecretvalue"
        for raw in (
            f"http://user:{marker}@10.0.0.1/",
            f"http://{marker}@10.0.0.1/",
            f"http://10.0.0.1/?token={marker}",
            f"http://10.0.0.1/#{marker}",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(LocalEndpointError) as caught:
                    normalize_base_url(raw)
                # A fixed sentence, never the value that was refused.
                self.assertEqual(caught.exception.reason, REASON_CREDENTIAL_IN_URL)
                self.assertNotIn(marker, caught.exception.reason)

    def test_non_http_schemes_are_refused(self) -> None:
        for raw in ("file:///etc/passwd", "gopher://10.0.0.1/", "ftp://10.0.0.1/", "10.0.0.1:11434"):
            with self.subTest(raw=raw):
                self.assert_refused(raw, REASON_SCHEME)

    def test_ports_outside_the_range_are_refused(self) -> None:
        for raw in ("http://10.0.0.1:0", "http://10.0.0.1:99999", "http://10.0.0.1:notaport"):
            with self.subTest(raw=raw):
                with self.assertRaises(LocalEndpointError) as caught:
                    normalize_base_url(raw)
                self.assertEqual(caught.exception.reason, REASON_PORT)

    def test_a_host_is_required(self) -> None:
        self.assert_refused("http://", REASON_HOST_MISSING)
        self.assert_refused("", REASON_HOST_MISSING)
        self.assert_refused("   ", REASON_HOST_MISSING)

    def test_wildcard_and_traversal_shapes_are_refused(self) -> None:
        self.assert_refused("http://*:8080", REASON_UNRESOLVED, resolver=resolver_returning())
        self.assert_refused("http://10.0.0.1/a/../b", REASON_PATH)
        self.assert_refused("http://10.0.0.1/%2e%2e", REASON_PATH)
        self.assert_refused("http://10.0.0.1//evil", REASON_PATH)

    def test_refusal_reasons_never_echo_the_url(self) -> None:
        secret = "http://user:hunter2-secret@10.0.0.1/?token=hunter2-secret"
        with self.assertRaises(LocalEndpointError) as caught:
            normalize_base_url(secret)
        self.assertNotIn("hunter2", caught.exception.reason)
        self.assertEqual(caught.exception.reason, REASON_CREDENTIAL_IN_URL)


class HostnameResolutionTests(unittest.TestCase):
    def test_all_private_answers_are_accepted(self) -> None:
        url = normalize_base_url(
            "http://dual.lan:8080",
            resolver=resolver_returning("192.168.1.5", "fd00::5", "127.0.0.1"),
        )
        self.assertEqual(url, "http://dual.lan:8080")

    def test_a_single_public_answer_refuses_the_whole_host(self) -> None:
        with self.assertRaises(LocalEndpointError) as caught:
            normalize_base_url(
                "http://mixed.lan:8080",
                resolver=resolver_returning("192.168.1.5", "93.184.216.34"),
            )
        self.assertEqual(caught.exception.reason, REASON_RESOLVE)

    def test_no_answers_is_a_refusal_not_a_pass(self) -> None:
        with self.assertRaises(LocalEndpointError) as caught:
            normalize_base_url("http://nowhere.lan:8080", resolver=resolver_returning())
        self.assertEqual(caught.exception.reason, REASON_UNRESOLVED)

    def test_metadata_in_the_answer_list_refuses_the_host(self) -> None:
        with self.assertRaises(LocalEndpointError) as caught:
            normalize_base_url(
                "http://evil.lan:8080",
                resolver=resolver_returning("169.254.169.254"),
            )
        self.assertEqual(caught.exception.reason, REASON_RESOLVE)


class _StubSocket:
    def __init__(self, address) -> None:
        self._address = address

    def getpeername(self):
        if self._address is None:
            raise OSError("not connected")
        return self._address


class PeerCheckTests(unittest.TestCase):
    def test_a_private_peer_passes(self) -> None:
        check = make_peer_check("ollama")
        check(_StubSocket(("127.0.0.1", 11434)))
        check(_StubSocket(("192.168.1.9", 8080)))
        check(_StubSocket(("fd00::1", 443, 0, 0)))

    def test_a_public_or_metadata_peer_is_refused_with_a_fixed_message(self) -> None:
        for address in (
            ("93.184.216.34", 80),
            ("169.254.169.254", 80),
            ("fe80::1", 80, 0, 0),
            ("0.0.0.0", 80),
            (None,),
        ):
            with self.subTest(address=address):
                with self.assertRaises(AdapterError) as caught:
                    make_peer_check("llama_cpp")(_StubSocket(address if address != (None,) else None))
                error = caught.exception
                self.assertEqual(error.error_class, ERROR_POLICY)
                self.assertEqual(error.provider_id, "llama_cpp")
                # One fixed gateway-authored sentence: it names neither the
                # address that was refused nor the one that was configured.
                self.assertEqual(
                    error.message,
                    "the configured local endpoint did not resolve to a private address",
                )

    def test_the_message_is_the_same_for_every_refused_peer(self) -> None:
        messages = set()
        for address in (("93.184.216.34", 80), ("169.254.169.254", 80)):
            with self.assertRaises(AdapterError) as caught:
                make_peer_check("ollama")(_StubSocket(address))
            messages.add(caught.exception.message)
        self.assertEqual(len(messages), 1)


class RebindingAndRedirectTests(unittest.TestCase):
    """Cases 4 and 5, against a real loopback server."""

    def setUp(self) -> None:
        self.fixture = LocalFixture()
        self.addCleanup(self.fixture.close)

    def test_a_refused_peer_writes_no_request_byte(self) -> None:
        """A rebound hostname is dropped before the request is written."""
        def refuse(_sock):
            raise AdapterError(
                ERROR_POLICY,
                "the configured local endpoint did not resolve to a private address",
                provider_id="ollama",
            )

        client = UpstreamClient(peer_check=refuse)
        self.addCleanup(client.close)
        with self.assertRaises(AdapterError) as caught:
            client.request(
                "GET",
                f"{self.fixture.base_url}/v1/models",
                headers={},
                provider_id="ollama",
            )
        self.assertEqual(caught.exception.error_class, ERROR_POLICY)
        # The server never saw a request: nothing was written.
        self.assertEqual(self.fixture.requests, [])

    def test_the_peer_check_is_installed_before_the_socket_factory(self) -> None:
        """The check is wired into connect(), i.e. before a TLS handshake."""
        client = UpstreamClient(peer_check=make_peer_check("ollama"))
        self.addCleanup(client.close)
        connection = client._pool._new(("http", "127.0.0.1", self.fixture.port), 5.0)
        self.assertIsNot(
            getattr(connection, "_create_connection", None), socket.create_connection
        )

    def test_a_client_without_a_peer_check_is_unchanged(self) -> None:
        client = UpstreamClient()
        self.addCleanup(client.close)
        connection = client._pool._new(("http", "127.0.0.1", self.fixture.port), 5.0)
        self.assertIs(connection._create_connection, socket.create_connection)

    def test_a_passing_peer_check_lets_the_request_through(self) -> None:
        client = UpstreamClient(peer_check=make_peer_check("ollama"))
        self.addCleanup(client.close)
        response = client.request(
            "GET",
            f"{self.fixture.base_url}/v1/models",
            headers={},
            provider_id="ollama",
            retry_safe=True,
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.fixture.requests), 1)

    def test_a_redirect_from_a_local_endpoint_is_refused_and_never_replayed(self) -> None:
        fixture = LocalFixture(redirect_to="http://93.184.216.34/steal")
        self.addCleanup(fixture.close)
        from gateway.adapters.local_openai import LocalOpenAIAdapter
        from gateway.config import ConfigStore, ProviderState
        import tempfile
        from pathlib import Path
        from gateway.credentials import CredentialStore

        with tempfile.TemporaryDirectory() as tmp:
            config = ConfigStore(Path(tmp) / "config.json")
            with config.mutate() as snapshot:
                snapshot.providers["ollama"] = ProviderState(
                    provider_id="ollama", kind="ollama", base_url=fixture.base_url
                )
            adapter = LocalOpenAIAdapter(
                "ollama",
                CredentialStore(Path(tmp)).provider_handle("ollama"),
                config,
            )
            from gateway.adapters.base import ProviderRequest

            with self.assertRaises(AdapterError) as caught:
                adapter.invoke(
                    ProviderRequest(model="local-test-model", payload={"messages": []}, dialect="openai_chat")
                )
            self.assertEqual(caught.exception.error_class, ERROR_POLICY)
            # Exactly the one request we made: the redirect target was never
            # requested, so no credential could have followed it.
            self.assertEqual(len(fixture.requests), 1)
            self.assertNotIn("93.184.216.34", "".join(fixture.paths()))


if __name__ == "__main__":
    unittest.main()
