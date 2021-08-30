import json
import os
import re
import uuid
from asyncio import current_task, get_event_loop
from typing import List, Union

import pendulum
import pydantic
import sqlalchemy as sa
from sqlalchemy import Column
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.event import listens_for
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_scoped_session,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import as_declarative, declared_attr, sessionmaker
from sqlalchemy.schema import MetaData
from sqlalchemy.sql.functions import FunctionElement
from sqlalchemy.sql.sqltypes import BOOLEAN
from sqlalchemy.types import CHAR, TypeDecorator, TypeEngine

from prefect import settings

camel_to_snake = re.compile(r"(?<!^)(?=[A-Z])")

ENGINES = {}
SESSION_FACTORIES = {}


async def get_engine(connection_url: str = None, echo: bool = None) -> sa.engine.Engine:
    """Retrieves an async SQLAlchemy engine.

    A new engine is created for each event loop and cached, so that engines are
    not shared across loops.

    If a sqlite in-memory database OR a non-existant sqlite file-based database
    is provided, it is automatically populated with database objects.

    Args:
        connection_url ([type], optional): The database connection string.
            Defaults to the value in Prefect's settings.
        echo ([type], optional): [description]. Whether to echo SQL sent
            to the database. Defaults to the value in Prefect's settings.

    Returns:
        sa.engine.Engine: a SQLAlchemy engine
    """
    if connection_url is None:
        connection_url = settings.orion.database.connection_url.get_secret_value()
    if echo is None:
        echo = settings.orion.database.echo

    kwargs = {}

    loop = get_event_loop()
    cache_key = (loop, connection_url, echo)
    if cache_key not in ENGINES:

        # ensure a long-lasting pool is used with in-memory databases
        # because they disappear when the last connection closes
        if connection_url.startswith("sqlite") and ":memory:" in connection_url:
            kwargs.update(poolclass=sa.pool.SingletonThreadPool)

        engine = create_async_engine(connection_url, echo=echo, **kwargs)

        # if this is a new sqlite database create all database objects
        if engine.dialect.name == "sqlite" and (
            ":memory:" in engine.url.database
            or "mode=memory" in engine.url.database
            or not os.path.exists(engine.url.database)
        ):
            await create_db(engine)

        ENGINES[cache_key] = engine
    return ENGINES[cache_key]


async def get_session_factory(
    bind: Union[sa.engine.Engine, sa.engine.Connection] = None,
) -> sa.ext.asyncio.scoping.async_scoped_session:
    """Retrieves a SQLAlchemy session factory for the provided bind.
    The session factory is cached for each event loop.

    Args:
        engine (Union[sa.engine.Engine, sa.engine.Connection], optional): An
            async SQLAlchemy engine or connection. If none is
            provided, `get_engine()` is called to recover one.

    Returns:
        sa.ext.asyncio.scoping.async_scoped_session: an async scoped session factory
    """
    if bind is None:
        bind = await get_engine()

    loop = get_event_loop()
    cache_key = (loop, bind)
    if cache_key not in SESSION_FACTORIES:
        # create session factory
        session_factory = sessionmaker(
            bind,
            future=True,
            expire_on_commit=False,
            class_=AsyncSession,
        )

        # create session factory with async scoping
        SESSION_FACTORIES[cache_key] = async_scoped_session(
            session_factory, scopefunc=current_task
        )

    return SESSION_FACTORIES[cache_key]


@listens_for(sa.engine.Engine, "engine_connect", once=True)
def setup_sqlite(conn, named=True):
    """The first time a connection is made to a sqlite engine, we enable sqlite foreign keys"""
    if conn.engine.url.get_backend_name() == "sqlite":
        # enable foreign keys
        cursor = conn.connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class UUIDDefault(FunctionElement):
    """
    Platform-independent UUID default generator.
    Note the actual functionality for this class is speficied in the
    `compiles`-decorated functions below
    """

    name = "uuid_default"


@compiles(UUIDDefault, "postgresql")
def visit_custom_uuid_default_for_postgres(element, compiler, **kwargs):
    """
    Generates a random UUID in Postgres; requires the pgcrypto extension.
    """

    return "(GEN_RANDOM_UUID())"


@compiles(UUIDDefault)
def visit_custom_uuid_default(element, compiler, **kwargs):
    """
    Generates a random UUID in other databases (SQLite) by concatenating
    bytes in a way that approximates a UUID hex representation. This is
    sufficient for our purposes of having a random client-generated ID
    that is compatible with a UUID spec.
    """

    return """
    (
        lower(hex(randomblob(4))) 
        || '-' 
        || lower(hex(randomblob(2))) 
        || '-4' 
        || substr(lower(hex(randomblob(2))),2) 
        || '-' 
        || substr('89ab',abs(random()) % 4 + 1, 1) 
        || substr(lower(hex(randomblob(2))),2) 
        || '-' 
        || lower(hex(randomblob(6)))
    )
    """


class Timestamp(TypeDecorator):
    """TypeDecorator that ensures that timestamps have a timezone.

    For SQLite, all timestamps are converted to UTC (since they are stored
    as naive timestamps) and recovered as UTC.

    Note: this should still be instantiated as Timestamp(timezone=True)
    """

    impl = sa.TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        else:
            if value.tzinfo is None:
                raise ValueError("Timestamps must have a timezone.")
            elif dialect.name == "sqlite":
                return pendulum.instance(value).in_timezone("UTC")
            else:
                return value

    def process_result_value(self, value, dialect):
        # retrieve timestamps in their native timezone (or UTC)
        if value is not None:
            return pendulum.instance(value).in_timezone("utc")


class UUID(TypeDecorator):
    """
    Platform-independent UUID type.

    Uses PostgreSQL's UUID type, otherwise uses
    CHAR(36), storing as stringified hex values with
    hyphens.
    """

    impl = TypeEngine
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID())
        else:
            return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        elif dialect.name == "postgresql":
            return str(value)
        elif isinstance(value, uuid.UUID):
            return str(value)
        else:
            return str(uuid.UUID(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        else:
            if not isinstance(value, uuid.UUID):
                value = uuid.UUID(value)
            return value


class JSON(TypeDecorator):
    """
    JSON type that returns SQLAlchemy's dialect-specific JSON types, where
    possible. Uses generic JSON otherwise.

    The "base" type is postgresql.JSONB to expose useful methods prior
    to SQL compilation
    """

    impl = postgresql.JSONB
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.JSONB())
        elif dialect.name == "sqlite":
            return dialect.type_descriptor(sqlite.JSON())
        else:
            return dialect.type_descriptor(sa.JSON())


class Pydantic(TypeDecorator):
    impl = JSON
    cache_ok = True

    def __init__(self, pydantic_type):
        super().__init__()
        self._pydantic_type = pydantic_type

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        # parse the value to ensure it complies with the schema
        # (this will raise validation errors if not)
        value = pydantic.parse_obj_as(self._pydantic_type, value)
        # sqlalchemy requires the bind parameter's value to be a python-native
        # collection of JSON-compatible objects. we achieve that by dumping the
        # value to a json string using the pydantic JSON encoder and re-parsing
        # it into a python-native form.
        return json.loads(json.dumps(value, default=pydantic.json.pydantic_encoder))

    def process_result_value(self, value, dialect):
        if value is not None:
            # load the json object into a fully hydrated typed object
            return pydantic.parse_obj_as(self._pydantic_type, value)


class now(FunctionElement):
    """
    Platform-independent "now" generator
    """

    name = "now"


@compiles(now, "sqlite")
def sqlite_microseconds_current_timestamp(element, compiler, **kwargs):
    """
    Generates the current timestamp for SQLite

    We need to add three zeros to the string representation
    because SQLAlchemy uses a regex expression which is expecting
    6 decimal places, but SQLite only stores milliseconds. This
    causes SQLAlchemy to interpret 01:23:45.678 as if it were
    01:23:45.000678. By forcing SQLite to store an extra three
    0's, we work around his issue.

    Note this only affects timestamps that we ask SQLite to issue
    in SQL (like the default value for a timestamp column); not
    datetimes provided by SQLAlchemy itself.
    """
    return "strftime('%Y-%m-%d %H:%M:%f000+00:00', 'now')"


@compiles(now)
def current_timestamp(element, compiler, **kwargs):
    """
    Generates the current timestamp in standard SQL
    """
    return "CURRENT_TIMESTAMP"


class json_contains(FunctionElement):
    type = BOOLEAN
    name = "json_contains"

    def __init__(self, json_expr, values: List):
        self.json_expr = json_expr
        self.values = values
        super().__init__()


@compiles(json_contains)
def json_contains_postgresql(element, compiler, **kwargs):
    return compiler.process(
        sa.type_coerce(element.json_expr, postgresql.JSONB).contains(element.values),
        **kwargs
    )


@compiles(json_contains, "sqlite")
def json_contains_sqlite(element, compiler, **kwargs):

    json_values = []
    for v in element.values:

        # sqlite appears to store JSON as a string with whitespace removed,
        # so non-scalar equality needs to be formatted identically
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"))
        json_values.append(v)

    json_each = sa.func.json_each(element.json_expr).alias("json_each")

    # attempt to match each of the provided values at least once
    return compiler.process(
        sa.and_(
            *[
                sa.select(1)
                .select_from(json_each)
                .filter(sa.literal_column("json_each.value") == v)
                .exists()
                for v in json_values
            ]
            or [True]
        ),
        **kwargs
    )


class json_has_any_key(FunctionElement):
    type = BOOLEAN
    name = "json_has_any_key"

    def __init__(self, json_expr, values: List):
        self.json_expr = json_expr
        if not all(isinstance(v, str) for v in values):
            raise ValueError("json_has_any_key values must be strings")
        self.values = values
        super().__init__()


@compiles(json_has_any_key)
def json_has_any_key_postgresql(element, compiler, **kwargs):

    values_array = postgresql.array(element.values)
    # if the array is empty, postgres requires a type annotation
    if not element.values:
        values_array = sa.func.cast(values_array, postgresql.ARRAY(sa.String))

    return compiler.process(
        sa.type_coerce(element.json_expr, postgresql.JSONB).has_any(values_array),
        **kwargs
    )


@compiles(json_has_any_key, "sqlite")
def json_has_any_key_sqlite(element, compiler, **kwargs):
    # attempt to match any of the provided values at least once
    json_each = sa.func.json_each(element.json_expr).alias("json_each")
    return compiler.process(
        sa.select(1)
        .select_from(json_each)
        .filter(sa.literal_column("json_each.value").in_(element.values))
        .exists(),
        **kwargs
    )


class json_has_all_keys(FunctionElement):
    type = BOOLEAN
    name = "json_has_all_keys"

    def __init__(self, json_expr, values: List):
        self.json_expr = json_expr
        if not all(isinstance(v, str) for v in values):
            raise ValueError("json_has_any_key values must be strings")
        self.values = values
        super().__init__()


@compiles(json_has_all_keys)
def json_has_all_keys_postgresql(element, compiler, **kwargs):
    values_array = postgresql.array(element.values)

    # if the array is empty, postgres requires a type annotation
    if not element.values:
        values_array = sa.func.cast(values_array, postgresql.ARRAY(sa.String))

    return compiler.process(
        sa.type_coerce(element.json_expr, postgresql.JSONB).has_all(values_array),
        **kwargs
    )


@compiles(json_has_all_keys, "sqlite")
def json_has_all_keys_sqlite(element, compiler, **kwargs):
    # attempt to match all of the provided values at least once
    # by applying an "any_key" match to each one individually
    return compiler.process(
        sa.and_(
            *[json_has_any_key(element.json_expr, [v]) for v in element.values]
            or [True]
        )
    )


# define naming conventions for our Base class to use
# sqlalchemy will use the following templated strings
# to generate the names of indices, constraints, and keys
#
# more information on this templating and available
# customization can be found here
# https://docs.sqlalchemy.org/en/14/core/metadata.html#sqlalchemy.schema.MetaData
#
# this also allows us to avoid having to specify names explicitly
# when using sa.ForeignKey.use_alter = True
# https://docs.sqlalchemy.org/en/14/core/constraints.html
base_metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_N_label)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


@as_declarative(metadata=base_metadata)
class Base(object):
    """
    Base SQLAlchemy model that automatically infers the table name
    and provides ID, created, and updated columns
    """

    # required in order to access columns with server defaults
    # or SQL expression defaults, subsequent to a flush, without
    # triggering an expired load
    #
    # this allows us to load attributes with a server default after
    # an INSERT, for example
    #
    # https://docs.sqlalchemy.org/en/14/orm/extensions/asyncio.html#preventing-implicit-io-when-using-asyncsession
    __mapper_args__ = {"eager_defaults": True}

    @declared_attr
    def __tablename__(cls):
        """
        By default, turn the model's camel-case class name
        into a snake-case table name. Override by providing
        an explicit `__tablename__` class property.
        """
        return camel_to_snake.sub("_", cls.__name__).lower()

    id = Column(
        UUID(),
        primary_key=True,
        server_default=UUIDDefault(),
        default=uuid.uuid4,
    )
    created = Column(
        Timestamp(timezone=True),
        nullable=False,
        server_default=now(),
        default=lambda: pendulum.now("UTC"),
    )
    updated = Column(
        Timestamp(timezone=True),
        nullable=False,
        index=True,
        server_default=now(),
        default=lambda: pendulum.now("UTC"),
        onupdate=now(),
    )


async def create_db(engine=None):
    engine = engine or await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db(engine=None):
    engine = engine or await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def get_dialect():
    connection_url = settings.orion.database.connection_url.get_secret_value()
    if connection_url.startswith("postgresql"):
        return "postgresql"
    elif connection_url.startswith("sqlite"):
        return "sqlite"
    else:
        raise ValueError("Unrecognized dialect")


# call immediately to verify runtime connection string
assert get_dialect()
