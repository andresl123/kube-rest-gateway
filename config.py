"""
config.py — Application configuration for kube-rest-gateway.

All values can be overridden via environment variables so you never
have to hard-code secrets into source code.
"""

import os
from dotenv import load_dotenv

# Load variables from .env file if it exists
load_dotenv()

# ── Kubernetes ──────────────────────────────────────────────────────────────
# Absolute path to the kubeconfig file the server will use.
# Default: ~/.kube/config (standard kubectl location).
KUBECONFIG_PATH: str = os.environ.get(
    "KUBECONFIG_PATH",
    os.path.expanduser("~/.kube/config"),
)

# ── API security ────────────────────────────────────────────────────────────
# A static bearer token that the AI agent must supply in every request.
# Set this to a long random string and share it only with the agent.
# Example: python -c "import secrets; print(secrets.token_hex(32))"
# Leave empty ("") to DISABLE authentication (not recommended in production).
API_TOKEN: str = os.environ.get("GATEWAY_API_TOKEN", "")

# ── Server ───────────────────────────────────────────────────────────────────
# The host interface and port the server listens on.
# Use 0.0.0.0 to listen on all interfaces (internal and external network).
HOST: str = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("GATEWAY_PORT", "8081"))

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO").upper()

# ── Defaults ─────────────────────────────────────────────────────────────────
# Default namespace used when no namespace is specified in a request.
DEFAULT_NAMESPACE: str = os.environ.get("DEFAULT_NAMESPACE", "default")

# Number of recent log lines to fetch when no tail_lines query-param is given.
DEFAULT_LOG_TAIL_LINES: int = int(os.environ.get("DEFAULT_LOG_TAIL_LINES", "100"))
