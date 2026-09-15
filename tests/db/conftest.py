import psycopg
import pytest
from scenario import Scenario


@pytest.fixture
def db(cur: psycopg.Cursor) -> Scenario:
    return Scenario(cur)
