import base64
import os
import uuid

import cv2
import numpy as np
import pytest
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from templateserver.config import Config
from templateserver.models import Base, CoordinationLock, State
from templateserver.store import Store


@pytest.fixture
def store():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a dedicated PostgreSQL database")
    instance = Store(url)
    schema = "test_" + uuid.uuid4().hex
    root = instance.engine
    with root.begin() as connection:
        connection.execute(CreateSchema(schema))
    instance.engine = root.execution_options(schema_translate_map={None: schema})
    instance.session = sessionmaker(instance.engine, expire_on_commit=False)
    Base.metadata.create_all(instance.engine)
    with instance.session.begin() as session:
        session.add(State(id=1))
        session.add_all([CoordinationLock(id=value) for value in (1, 2, 3)])
    yield instance
    with root.begin() as connection:
        connection.execute(DropSchema(schema, cascade=True))
    instance.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        database_url="",
        admin_token="test-admin",
        event_token="test-events",
        data_dir=tmp_path,
        main_event_url="https://main.example/events",
    )


def encoded_image(seed=3, size=49, brightness=0):
    image = np.random.default_rng(seed).integers(10, 230, (size, size, 3), dtype=np.uint8)
    image = np.clip(image.astype(int) + brightness, 0, 255).astype(np.uint8)
    return base64.b64encode(cv2.imencode(".png", image)[1]).decode()
