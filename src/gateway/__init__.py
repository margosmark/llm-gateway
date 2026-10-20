"""Personal LLM Gateway.

A personal, LAN-only OpenAI-compatible LLM gateway.

Design rules that the whole package depends on:

* LAN-only. The listener binds an explicit private address or loopback.
  Wildcard binds are refused at startup.
* Standard library only. No third-party runtime dependency exists.
* No credential ever appears in a response body, log, export, analytics
  record, URL, error message, or image layer.
* No prompt or response content is ever persisted.
* Nothing here is a provider-specific branch in the routing core; providers
  are adapters behind a single protocol.
"""

__all__ = ["__version__"]

__version__ = "2.0.0"
