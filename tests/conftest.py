"""Keep test sockets short.

A Unix socket path is capped near 108 bytes. This box sets TMPDIR to a deep directory,
and pytest's per-test tmp_path then pushes the marmot tests' socket paths past the cap —
they fail with ENAMETOOLONG on a green tree. Redirect the temp base before pytest creates
any tmp_path directory.
"""
import os
import tempfile

if len(tempfile.gettempdir()) > 20:
    os.environ["TMPDIR"] = "/tmp"
    tempfile.tempdir = None
