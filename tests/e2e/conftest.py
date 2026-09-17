import socket

import pytest
from resolver.conftest import Resolver


@pytest.fixture(scope="session")
def resolver() -> Resolver:
    return Resolver(socket.gethostbyname("resolver"))
