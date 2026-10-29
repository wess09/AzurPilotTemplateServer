import os

from alembic import context
from sqlalchemy import create_engine

from templateserver.models import Base
from templateserver.store import database_url

engine = create_engine(database_url(os.environ["DATABASE_URL"]), hide_parameters=True)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
