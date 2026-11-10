# dots-bridge Contributor Instructions

- Before adding a transport or connector path, trace the existing bridge, client, and store calls. Reuse their request and message types, authorization checks, and lifecycle operations. Keep transport-specific framing and error mapping in the connector.
- Keep request completion, cancellation, and idempotency in the existing bridge and store. Before adding state or a lock, identify the failure or race it protects. Verify that the affected transports preserve delivery and cancellation behavior when requests overlap, are repeated, or resume after a disconnect.
- Use temporary SQLite databases and fake callbacks for tests. Follow the [development checks](README.md#development) for code changes.
