"""Verification-only network barrier: no real providers or databases."""
import socket
import pytest

@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("CARA verification forbids network connections")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", connect)
