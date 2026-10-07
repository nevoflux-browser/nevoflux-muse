"""Connect a Muse agent to a NevoFlux browser: MCP over an E2E-encrypted relay."""

__version__ = "0.2.0"

# The agent-channel protocol versions this client speaks. The head reports its
# own in initialize -> capabilities.experimental.nevoflux.protocol.
PROTOCOL_MIN = 1
PROTOCOL_MAX = 1
