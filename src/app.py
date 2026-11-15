import base64
import json
import os
import re
import traceback
from http import HTTPStatus

from data import formats
from data.generator import TYPES, generate_rows

# =================================================
# CONFIG
# =================================================

LOCAL = os.environ["MOCKGEN_LOCAL"] == "true"

# Local mode swaps in SQLite stand-ins with the same functions, so AWS code is never imported
if LOCAL:
    from local import backend as databases
    from local import backend as writers
    from local import catalog

    # Nothing local raises botocore errors; this keeps the except clause below valid
    class ClientError(Exception):
        pass

else:
    from botocore.exceptions import ClientError

    from aws import catalog, databases, writers

SOURCES = ("postgres", "redshift", "dynamodb", "s3", "glue")
SQL_SOURCES = ("postgres", "redshift")

TABLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
DATABASE_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}(/[a-z0-9][a-z0-9_-]{0,62}){0,9}$")

MAX_ROWS = 100000
MAX_COLUMNS = 100
MAX_CELLS = 1000000
MAX_BATCHES = 1000
PREVIEW_ROWS = 10

# Static files for the UI: every file in ui/ is served at /<name>, index.html at /
UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
CONTENT_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}
STATIC = {}
for _name in os.listdir(UI_DIR):
    with open(os.path.join(UI_DIR, _name), encoding="utf-8") as f:
        STATIC["/" if _name == "index.html" else "/" + _name] = (f.read(), CONTENT_TYPES[os.path.splitext(_name)[1]] + "; charset=utf-8")

# =================================================
# HTTP HELPERS
# =================================================


# Build an ALB response
def response(status: int, body, content_type: str = "application/json") -> dict:
    """
    Build a response in the format the ALB expects from a Lambda target.

    Arguments:
        status (int): HTTP status code
        body: str for static files, anything JSON-serializable otherwise
        content_type (str): Content-Type header

    Returns:
        dict: ALB Lambda response
    """
    headers = {"Content-Type": content_type, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
    text = body if isinstance(body, str) else json.dumps(body, default=str)
    return {
        "statusCode": status,
        "statusDescription": f"{status} {HTTPStatus(status).phrase}",
        "isBase64Encoded": False,
        "headers": headers,
        "body": text,
    }


# =================================================
# VALIDATION (everything from the browser is untrusted)
# =================================================


# Check a name against a pattern
def check_name(value, pattern: re.Pattern, label: str) -> str:
    """
    Validate a name from the request.

    Arguments:
        value: value from the request
        pattern (re.Pattern): allowed pattern
        label (str): what the name is, for the error message

    Returns:
        str: the validated name
    """
    if not isinstance(value, str) or not pattern.match(value):
        allowed = "_" if pattern is TABLE_RE else "-, _ and / between folders" if pattern is PREFIX_RE else "-"
        raise ValueError(f"Invalid {label}. Use lowercase letters, digits and {allowed}")
    return value


# Check a whole number within a range
def check_int(value, low: int, high: int, label: str) -> int:
    """
    Validate an integer from the request.

    Arguments:
        value: value from the request
        low (int): minimum
        high (int): maximum
        label (str): field name for the error message

    Returns:
        int: the validated number
    """
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}")
    return value


# Whether a destination accepts values of the wrong type
def allows_wrong_types(source: str, fmt) -> bool:
    """
    Only schemaless destinations can hold wrong-type junk: DynamoDB, and S3 as CSV or JSON.
    SQL tables, Parquet, Avro and Glue tables have fixed column types.

    Arguments:
        source (str): source type
        fmt: file format for s3/glue, None otherwise

    Returns:
        bool: True if wrong-type junk is allowed
    """
    return source == "dynamodb" or (source == "s3" and fmt not in formats.TYPED_FORMATS)


# Check the column definitions
def check_columns(raw, source: str) -> list:
    """
    Validate column definitions.

    Arguments:
        raw: columns from the request
        source (str): target source type

    Returns:
        list: clean column dicts with name, type, null_pct, junk_pct
    """
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_COLUMNS:
        raise ValueError(f"Define between 1 and {MAX_COLUMNS} columns")
    columns = []
    seen = set()
    for col in raw:
        if not isinstance(col, dict):
            raise ValueError("Invalid column")
        # Name
        name = check_name(col.get("name"), TABLE_RE, "column name")
        if name in seen:
            raise ValueError(f"Column {name} is defined twice")
        if source == "dynamodb" and name == "pk":
            raise ValueError("Column name pk is reserved for the DynamoDB partition key")
        seen.add(name)
        # Type
        if col.get("type") not in TYPES:
            raise ValueError(f"Column {name} has an unknown type")
        # Null and junk shares
        null_pct = check_int(col.get("null_pct"), 0, 100, f"{name} null %")
        junk_pct = check_int(col.get("junk_pct"), 0, 100, f"{name} junk %")
        if null_pct + junk_pct > 100:
            raise ValueError(f"{name}: null % plus junk % can't be more than 100")
        columns.append({"name": name, "type": col["type"], "null_pct": null_pct, "junk_pct": junk_pct})
    return columns


# Validate where data goes
def check_destination(body: dict, need_table: bool, need_format: bool) -> dict:
    """
    Validate the destination fields that apply to the chosen source.

    Arguments:
        body (dict): request body
        need_table (bool): whether a table name (for S3: the prefix) is required
        need_format (bool): whether a file format is required (s3/glue)

    Returns:
        dict: source plus the applicable fields: db + database + schema (SQL), prefix (s3), format (s3/glue), table (not s3).
              S3 data always goes under mock-data/ and Glue tables under mock-tables/ in the app bucket
    """
    source = body.get("source")
    if source not in SOURCES:
        raise ValueError("Unknown data source")
    dest = {"source": source}

    # Postgres / Redshift: an admin-created database plus a schema
    if source in SQL_SOURCES:
        dest["database"] = check_name(body.get("database"), DATABASE_RE, "database name")
        dest["db"] = databases.get_database(dest["database"], source)
        if need_table:
            dest["schema"] = check_name(body.get("schema"), TABLE_RE, "schema name")

    # S3: the prefix (a folder under mock-data/) is the dataset, so there's no table name
    if source == "s3" and need_table:
        dest["prefix"] = check_name(body.get("prefix"), PREFIX_RE, "prefix")

    # S3 / Glue: file format, only needed to write
    if source in ("s3", "glue") and need_format:
        if body.get("format") not in formats.FORMATS:
            raise ValueError("Pick a file format")
        dest["format"] = body["format"]

    if need_table and source != "s3":
        dest["table"] = check_name(body.get("table"), TABLE_RE, "table name")
    return dest


# =================================================
# ACTIONS
# =================================================


# Run one API action (no accounts: everyone can do everything)
def run_action(action: str, body: dict):
    """
    Dispatch an API action.

    Arguments:
        action (str): action name
        body (dict): request body

    Returns:
        JSON-serializable result
    """
    # App info for the UI
    if action == "info":
        return {"local": LOCAL}

    # What users can pick from: ready databases, where S3/Glue data goes, column types and formats
    if action == "sources":
        ready = []
        for db in databases.list_databases():
            if db["status"] == "available":
                ready.append({"name": db["name"], "engine": db["engine"]})
        return {
            "databases": ready,
            "s3_root": writers.S3_ROOT,
            "glue_root": writers.GLUE_ROOT,
            "glue_database": writers.GLUE_DATABASE,
            "types": list(TYPES),
            "formats": list(formats.FORMATS),
        }

    # Every dataset across all sources, with metadata (the datasets modal)
    if action == "catalog":
        dbs = []
        for db in databases.list_databases():
            if db["status"] == "available":
                dbs.append(databases.get_database(db["name"], db["engine"]))
        return catalog.catalog(dbs)

    # First rows of one dataset
    if action == "sample":
        return catalog.sample(check_destination(body, True, False))

    # What already exists at a location (fills the dropdowns)
    if action == "browse":
        return writers.browse(check_destination(body, False, False))

    # Sample rows for the preview panel, following the same junk rules as the real run
    if action == "preview":
        source = body.get("source")
        if source not in SOURCES:
            raise ValueError("Unknown data source")
        columns = check_columns(body.get("columns"), source)
        return {"rows": generate_rows(columns, PREVIEW_ROWS, allows_wrong_types(source, body.get("format")))}

    # Generate data in batches, creating or replacing the table
    if action == "generate":
        dest = check_destination(body, True, True)
        columns = check_columns(body.get("columns"), dest["source"])
        row_count = check_int(body.get("rows"), 1, MAX_ROWS, "Rows")
        if row_count * len(columns) > MAX_CELLS:
            raise ValueError(f"Rows x columns can't be more than {MAX_CELLS:,}")
        batch_size = check_int(body.get("batch_size") or row_count, 1, row_count, "Batch size")
        batch_count = -(-row_count // batch_size)
        if batch_count > MAX_BATCHES:
            raise ValueError(f"That makes {batch_count:,} batches. The limit is {MAX_BATCHES:,}; use a bigger batch size")
        allow_wrong = allows_wrong_types(dest["source"], dest.get("format"))

        # Batches are generated lazily so only one batch is in memory at a time
        def batches():
            remaining = row_count
            while remaining > 0:
                size = min(batch_size, remaining)
                remaining -= size
                yield generate_rows(columns, size, allow_wrong)

        info = writers.write(dest, columns, batches())
        if dest["source"] in SQL_SOURCES:
            info = {**databases.connection_info(dest["db"]), **info}
        return {"rows": row_count, "batches": batch_count, "connection": info}

    # Delete a table or dataset
    if action == "delete_table":
        writers.delete_table(check_destination(body, True, False))
        return {"ok": True}

    # Databases (Postgres / Redshift servers)
    if action == "databases":
        return {"databases": databases.list_databases()}
    if action == "create_database":
        engine = body.get("engine")
        if engine not in databases.ENGINES:
            raise ValueError("Engine must be postgres or redshift")
        return databases.create_database(check_name(body.get("name"), DATABASE_RE, "database name"), engine)
    if action == "refresh_database":
        return databases.refresh_database(check_name(body.get("name"), DATABASE_RE, "database name"))
    if action == "delete_database":
        return databases.delete_database(check_name(body.get("name"), DATABASE_RE, "database name"))

    raise ValueError("Unknown action")


# =================================================
# ENTRY POINT
# =================================================


# Lambda handler for ALB requests
def handler(event: dict, context) -> dict:
    """
    Serve the UI files on GET and the JSON API on POST /api.

    Arguments:
        event (dict): ALB request event
        context: Lambda context

    Returns:
        dict: ALB response
    """
    method = event["httpMethod"]
    path = event["path"]

    # UI files
    if method == "GET" and path in STATIC:
        content, content_type = STATIC[path]
        return response(200, content, content_type)
    if method != "POST" or path != "/api":
        return response(404, {"error": "Not found"})

    # Parse the JSON body
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return response(400, {"error": "Body must be JSON"})
    if not isinstance(body, dict):
        return response(400, {"error": "Body must be a JSON object"})
    action = body.get("action")

    try:
        return response(200, run_action(action, body))

    # Bad input and AWS errors go back to the UI as messages
    except ValueError as e:
        return response(400, {"error": str(e)})
    except ClientError as e:
        return response(400, {"error": e.response["Error"]["Message"]})
    # Anything else (e.g. a database driver error): log the traceback to CloudWatch and show the message,
    # instead of crashing the Lambda, which the load balancer turns into a bare 502
    except Exception as e:
        traceback.print_exc()
        return response(500, {"error": f"{type(e).__name__}: {e}"})
