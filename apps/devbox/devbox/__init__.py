"""a2a devbox — WebSocket ⇄ sshd bridge for the throwaway SSH dev box.

The box runs sshd bound to localhost only; this package's bridge is the sole
network-facing process (the Knative serving port), so SSH reaches the box over
the platform's HTTP(S) ingress via a WebSocket — no raw TCP ingress needed.
"""

__version__ = "0.1.0"
