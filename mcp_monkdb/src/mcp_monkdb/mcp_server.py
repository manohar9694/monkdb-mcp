import atexit
from dataclasses import asdict, dataclass, field, is_dataclass
import json
import logging
from typing import Any, List
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from monkdb import client

import concurrent

from mcp_monkdb.env_vals import get_config
from mcp_monkdb.models import Column, Table


MCP_SERVER_NAME = "mcp-monkdb"

# Configure logging for mcp server operations
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(MCP_SERVER_NAME)

QUERY_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=10)
atexit.register(lambda: QUERY_EXECUTOR.shutdown(wait=True))
SELECT_QUERY_TIMEOUT_SECS = 30


load_dotenv()

deps = [
    "monkdb",
    "python-dotenv",
    "uvicorn",
    "pip-system-certs",
]

mcp = FastMCP(MCP_SERVER_NAME, dependencies=deps)


def result_to_column(query_columns, result) -> List[Column]:
    return [Column(**dict(zip(query_columns, row))) for row in result]


def result_to_table(query_columns, result) -> List[Table]:
    return [Table(**dict(zip(query_columns, row))) for row in result]


def to_json(obj: Any) -> str:
    if is_dataclass(obj):
        return json.dumps(asdict(obj), default=to_json)
    elif isinstance(obj, list):
        return [to_json(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: to_json(value) for key, value in obj.items()}
    return obj


@mcp.tool()
def list_tables():
    """List available MonkDB tables under monkdb schema"""
    logger.info("Listing all tables")
    cursor = create_monkdb_client()
    try:
        cursor.execute("""
            SELECT table_name FROM information_schema.tables WHERE table_schema = 'monkdb';
        """)
        rows = cursor.fetchall()
        if not rows:
            raise ValueError("No tables found in schema 'monkdb'")
        logger.info(f"Found {len(rows)} tables")
        return [{"table_name": row[0]} for row in rows]
    except Exception as e:
        logger.error(f"Failed to list tables: {str(e)}")
        return {"status": "error", "message": str(e)}


def execute_query(query: str):
    cursor = create_monkdb_client()
    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        column_names = [desc[0] for desc in cursor.description]

        result = []
        for row in rows:
            result.append(
                {column_names[i]: value for i, value in enumerate(row)})

        logger.info(f"Query returned {len(result)} rows")
        return result

    except Exception as err:
        logger.error(f"Error executing query: {err}")
        return {"status": "error", "message": f"Query failed: {str(err)}"}


@mcp.tool()
def run_select_query(query: str):
    """Run a SELECT query in a MonkDB database"""
    logger.info(f"Executing SELECT query: {query}")

    # Enforce SELECT-only query for safety
    if not query.strip().lower().startswith("select"):
        logger.warning("Rejected non-SELECT query")
        return {
            "status": "error",
            "message": "Only SELECT queries are allowed in this endpoint.",
        }

    try:
        future = QUERY_EXECUTOR.submit(execute_query, query)
        try:
            result = future.result(timeout=SELECT_QUERY_TIMEOUT_SECS)

            if isinstance(result, dict) and "error" in result:
                logger.warning(f"Query failed: {result['error']}")
                return {
                    "status": "error",
                    "message": f"Query failed: {result['error']}",
                }

            return result
        except concurrent.futures.TimeoutError:
            logger.warning(
                f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds: {query}"
            )
            future.cancel()
            return {
                "status": "error",
                "message": f"Query timed out after {SELECT_QUERY_TIMEOUT_SECS} seconds",
            }
    except Exception as e:
        logger.error(f"Unexpected error in run_select_query: {str(e)}")
        return {"status": "error", "message": f"Unexpected error: {str(e)}"}


@mcp.tool()
def health_check():
    """Simple health check on MonkDB"""
    try:
        cursor = create_monkdb_client()
        cursor.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
def get_server_version():
    """Returns the version of MonkDB server using version() scalar function"""
    cursor = create_monkdb_client()
    result = cursor.execute("SELECT version() AS version")
    return result


@mcp.tool()
def describe_table(table_name: str):
    """Describe a table's columns in MonkDB"""
    cursor = create_monkdb_client()
    try:
        query = """
            SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'monkdb' AND table_name = %s;
        """
        cursor.execute(query, (table_name,))
        rows = cursor.fetchall()
        if not rows:
            raise ValueError(f"No columns found for table: {table_name}")

        query_columns = [desc[0] for desc in cursor.description]
        return result_to_column(query_columns, rows)

    except Exception as e:
        logger.error(f"Failed to describe table '{table_name}': {str(e)}")
        return {"status": "error", "message": str(e)}


def create_monkdb_client():
    config = get_config().get_client_config()
    try:
        connection = client.connect(config["url"], username=config["username"])
        logger.info("MonkDB connection established successfully!")
        return connection.cursor()
    except Exception as e:
        logger.error(f"Failed to connect to MonkDB: {str(e)}")
        raise
