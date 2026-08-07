"""Identity of this SDK, and the contract version it implements.

META-001: the spec version an SDK implements must be recorded in a
machine-readable form alongside its conformance level.
"""

SDK_NAME = "restless-sdk-python"  # WIRE-016: distinct per implementation
SDK_VERSION = "0.1.0"

#: The spec/CONTRACT.md version this SDK is verified against.
SPEC_VERSION = "1.0.0"

#: CONTRACT.md 1.1. "L1" = pure functions; "L2" = plus batching, caches,
#: injection and the safety guarantees.
CONFORMANCE_LEVEL = "L2"
