"""Mock of the Maxis agent gateway (roaming + WhatsApp tools) with DEMO DATA.

Stands in for the real gateway until Maxis shares it. See app.py for how to serve it.
"""

from .app import gateway, handle, httpx_transport
from .data import TEST_CALLERS

__all__ = ["TEST_CALLERS", "gateway", "handle", "httpx_transport"]
