# Library logging

Approved scope: host-controlled Python logging suitable for Home Assistant custom
integrations, useful debug diagnostics, and privacy-safe concurrent-device context.

Use module loggers under `govee_ble_client` with a package `NullHandler`.
Never configure application handlers, levels, formatters, or propagation. Emit
routine lifecycle and operation details at DEBUG; reserve WARNING for isolated
callback failures that otherwise disappear. Propagating errors remain DEBUG so
the application can decide when to report availability failures.

Use temporary connection tokens unrelated to device identity. Include operation,
elapsed time, configured timeout, sizes, handshake stage, and safe error class
names. Exclude exception messages/tracebacks, addresses, names, UUID arguments,
payloads, keys, decoded values, and raw traffic. Preserve all async behavior.

Approaches: configuration-only would leave debugging gaps; configurable raw
traffic logging would add privacy risk. The approved approach combines passive
library configuration with metadata-only diagnostics. No public logging API or
new dependency is needed. Standalone scripts retain their interactive output.

Validate host configuration preservation, output routing, privacy, concurrent
correlation, cancellation, and failure diagnostics with offline tests. Document
standalone and Home Assistant setup and the limits of dependency logging.
