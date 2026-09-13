"""Agent-side transport and storage.

Layout and boundary rules:
- ``protocol.py`` and ``runtime.py`` handle the ACP/JSON-RPC wire and the agent process.
  They must not import the database or any business module.
- ``events.py`` is the only module here allowed to touch persistence, and only the agent-owned
  ``agent_event`` / ``agent_session`` tables. No dataset, draft, tag, proposal, message or
  conversation model may appear anywhere in this package.
- ``stream.py`` turns the log into SSE and never writes. ``backend.py`` declares the agent
  backend contract, and ``bridge.py`` drives a backend and records what it reports; neither may
  know a concrete agent runtime, read a process stream, parse a wire format or serve HTTP.
"""
