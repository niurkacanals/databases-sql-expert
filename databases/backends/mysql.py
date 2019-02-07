import getpass
import logging
import typing
import uuid
from types import TracebackType

import aiomysql
from sqlalchemy.dialects.mysql import pymysql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql import ClauseElement

from databases.core import DatabaseURL
from databases.interfaces import DatabaseBackend, DatabaseSession, DatabaseTransaction

logger = logging.getLogger("databases")


class MySQLBackend(DatabaseBackend):
    def __init__(self, database_url: typing.Union[str, DatabaseURL]) -> None:
        self.database_url = DatabaseURL(database_url)
        self.dialect = self._get_dialect()
        self.pool = None

    def _get_dialect(self) -> Dialect:
        return pymysql.dialect(paramstyle="pyformat")

    async def connect(self) -> None:
        db = self.database_url
        self.pool = await aiomysql.create_pool(
            host=db.hostname,
            port=db.port or 3306,
            user=db.username or getpass.getuser(),
            password=db.password,
            db=db.database,
        )

    async def disconnect(self) -> None:
        assert self.pool is not None, "DatabaseBackend is not running"
        self.pool.close()
        await self.pool.wait_closed()
        self.pool = None

    def session(self, rollback_isolation: bool = False) -> "MySQLSession":
        assert self.pool is not None, "DatabaseBackend is not running"
        return MySQLSession(self.pool, self.dialect, rollback_isolation)


class Record:
    def __init__(self, row: tuple, result_columns: tuple) -> None:
        self._row = row
        self._result_columns = result_columns
        self._column_map = {
            col[0]: (idx, col) for idx, col in enumerate(self._result_columns)
        }

    def __getitem__(self, key: str) -> typing.Any:
        idx, col = self._column_map[key]
        raw = self._row[idx]
        return col[-1].python_type(raw)


class MySQLSession(DatabaseSession):
    def __init__(
        self,
        pool: aiomysql.pool.Pool,
        dialect: Dialect,
        rollback_isolation: bool = False,
    ):
        self.pool = pool
        self.dialect = dialect
        self.conn = None
        self.connection_holders = 0
        self.has_root_transaction = False
        self._rollback_transaction = None
        if rollback_isolation:
            self._rollback_transaction = self.transaction()

    async def __aenter__(self) -> DatabaseSession:
        if self._rollback_transaction is not None:
            await self._rollback_transaction.start()
        return self

    async def __aexit__(
        self,
        exc_type: typing.Type[BaseException] = None,
        exc_value: BaseException = None,
        traceback: TracebackType = None,
    ) -> None:
        if self._rollback_transaction is not None:
            await self._rollback_transaction.rollback()

    def _compile(self, query: ClauseElement) -> typing.Tuple[str, list, tuple]:
        compiled = query.compile(dialect=self.dialect)
        args = compiled.construct_params()
        logger.debug(compiled.string, args)
        return compiled.string, args, compiled._result_columns

    async def fetchall(self, query: ClauseElement) -> typing.Any:
        query, args, result_columns = self._compile(query)

        conn = await self.acquire_connection()
        cursor = await conn.cursor()
        try:
            await cursor.execute(query, args)
            rows = await cursor.fetchall()
            return [Record(row, result_columns) for row in rows]
        finally:
            await cursor.close()
            await self.release_connection()

    async def fetchone(self, query: ClauseElement) -> typing.Any:
        query, args, result_columns = self._compile(query)

        conn = await self.acquire_connection()
        cursor = await conn.cursor()
        try:
            await cursor.execute(query, args)
            row = await cursor.fetchone()
            return Record(row, result_columns)
        finally:
            await cursor.close()
            await self.release_connection()

    async def execute(self, query: ClauseElement) -> None:
        query, args, result_columns = self._compile(query)

        conn = await self.acquire_connection()
        cursor = await conn.cursor()
        try:
            await cursor.execute(query, args)
        finally:
            await cursor.close()
            await self.release_connection()

    async def executemany(self, query: ClauseElement, values: list) -> None:
        conn = await self.acquire_connection()
        cursor = await conn.cursor()
        try:
            for item in values:
                single_query = query.values(item)
                single_query, args, result_columns = self._compile(single_query)
                await cursor.execute(single_query, args)
        finally:
            await cursor.close()
            await self.release_connection()

    def transaction(self) -> DatabaseTransaction:
        return MySQLTransaction(self)

    async def acquire_connection(self) -> aiomysql.Connection:
        """
        Either acquire a connection from the pool, or return the
        existing connection. Must be followed by a corresponding
        call to `release_connection`.
        """
        self.connection_holders += 1
        if self.conn is None:
            self.conn = await self.pool.acquire()
        return self.conn

    async def release_connection(self) -> None:
        self.connection_holders -= 1
        if self.connection_holders == 0:
            await self.pool.release(self.conn)
            self.conn = None


class MySQLTransaction(DatabaseTransaction):
    def __init__(self, session: MySQLSession):
        self.session = session
        self.is_root = False

    async def start(self) -> None:
        if self.session.has_root_transaction is False:
            self.session.has_root_transaction = True
            self.is_root = True

        self.conn = await self.session.acquire_connection()
        if self.is_root:
            await self.conn.begin()
        else:
            id = str(uuid.uuid4()).replace("-", "_")
            self.savepoint_name = f"STARLETTE_SAVEPOINT_{id}"
            cursor = await self.conn.cursor()
            try:
                await cursor.execute(f"SAVEPOINT {self.savepoint_name}")
            finally:
                await cursor.close()

    async def commit(self) -> None:
        if self.is_root:  # pragma: no cover
            # In test cases the root transaction is never committed,
            # since we *always* wrap the test case up in a transaction
            # and rollback to a clean state at the end.
            await self.conn.commit()
            self.session.has_root_transaction = False
        else:
            cursor = await self.conn.cursor()
            try:
                await cursor.execute(f"RELEASE SAVEPOINT {self.savepoint_name}")
            finally:
                await cursor.close()
        await self.session.release_connection()

    async def rollback(self) -> None:
        if self.is_root:
            await self.conn.rollback()
            self.session.has_root_transaction = False
        else:
            cursor = await self.conn.cursor()
            try:
                await cursor.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint_name}")
            finally:
                await cursor.close()
        await self.session.release_connection()
