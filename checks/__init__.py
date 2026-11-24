"""Built-in static code and security checks for ACP Software Factory."""
from .secret_scan import scan_directory, SecretFinding

__all__ = ["scan_directory", "SecretFinding"]
