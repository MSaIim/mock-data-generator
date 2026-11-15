import json
import os
import re
import ssl
import time
import uuid
from decimal import Decimal

import boto3
import pg8000.native
from botocore.exceptions import ClientError

from data import formats
from data.generator import TYPES

# =================================================
# CONFIG AND CLIENTS
# =================================================

REGION = os.environ["AWS_REGION"]
STAGING_BUCKET = os.environ["STAGING_BUCKET"]
BUCKET_PREFIX = os.environ["BUCKET_PREFIX"]
REDSHIFT_ROLE_ARN = os.environ["REDSHIFT_ROLE_ARN"]
DYNAMO_PREFIX = "mockgen-"
GLUE_PREFIX = "mockgen_"

# Size of each multi-row Postgres INSERT
PG_STATEMENT_CHARS = 1000000

# Dataset files look like <prefix>/<table>/part-00000.<ext>
PART_RE = re.compile(r"^(?:(.+)/)?([^/]+)/part-\d{5}\.[a-z]+$")

# Every schema and its tables (Postgres and Redshift), excluding system schemas
SCHEMAS_SQL = (
    "SELECT n.nspname, c.relname FROM pg_namespace n "
    "LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relkind = 'r' "
    "WHERE substring(n.nspname, 1, 3) <> 'pg_' AND n.nspname <> 'information_schema' "
    "ORDER BY 1, 2"
)

# How Athena reads each file format
GLUE_FORMATS = {
    "csv": {
        "serde": "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
        "params": {"field.delim": ",", "escape.delim": "\\", "serialization.null.format": ""},
        "input": "org.apache.hadoop.mapred.TextInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
    },
    "json": {
        "serde": "org.openx.data.jsonserde.JsonSerDe",
        "params": {},
        "input": "org.apache.hadoop.mapred.TextInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
    },
    "parquet": {
        "serde": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
        "params": {},
        "input": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
    },
    "avro": {
        "serde": "org.apache.hadoop.hive.serde2.avro.AvroSerDe",
        "params": {},
        "input": "org.apache.hadoop.hive.ql.io.avro.AvroContainerInputFormat",
        "output": "org.apache.hadoop.hive.ql.io.avro.AvroContainerOutputFormat",
    },
}

s3 = boto3.client("s3")
dynamo = boto3.client("dynamodb")
dynamo_resource = boto3.resource("dynamodb")
glue = boto3.client("glue")
redshift_data = boto3.client("redshift-data")

# =================================================
# SQL HELPERS
# =================================================


# Open a connection to an Aurora Postgres cluster
def pg_connect(db: dict) -> pg8000.native.Connection:
    """
    Connect to Postgres. A paused cluster (0 ACU) takes a few seconds to wake up,
    so this retries for about a minute.

    Arguments:
        db (dict): database record from databases.get_database

    Returns:
        pg8000.native.Connection: open connection
    """
    # Encrypted in transit. The RDS CA isn't in Lambda's trust store, so the certificate isn't verified (traffic stays in the VPC)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(6):
        try:
            return pg8000.native.Connection(
                user=db["username"],
                password=db["password"],
                host=db["host"],
                port=int(db["port"]),
                database=db["database"],
                ssl_context=ctx,
                timeout=30,
            )
        except (pg8000.native.InterfaceError, OSError):
            if attempt == 5:
                raise
            time.sleep(10)


# Run SQL statements on Postgres and return the rows of the last one
def pg_run(db: dict, sqls: list) -> list:
    """
    Open a connection, run the statements in order, and close it.

    Arguments:
        db (dict): database record
        sqls (list): statements to run

    Returns:
        list: rows returned by the last statement
    """
    con = pg_connect(db)
    try:
        rows = []
        for sql in sqls:
            rows = con.run(sql) or []
        return rows
    except pg8000.native.DatabaseError as e:
        raise ValueError(f"Postgres: {e}")
    finally:
        con.close()


# Run SQL on Redshift Serverless through the Data API and wait for it to finish
def rs_exec(db: dict, sqls: list) -> tuple:
    """
    Run one statement, or several as a single transaction, and wait for the result.
    Signs in as the Lambda's IAM role (no stored secret needed).

    Arguments:
        db (dict): database record
        sqls (list): statements to run

    Returns:
        tuple: (column names, rows as lists of plain values) for a single statement; ([], []) for batches
    """
    # Submit the statement(s)
    target = {"WorkgroupName": db["ident"], "Database": db["database"]}
    if len(sqls) == 1:
        statement_id = redshift_data.execute_statement(Sql=sqls[0], **target)["Id"]
    else:
        statement_id = redshift_data.batch_execute_statement(Sqls=sqls, **target)["Id"]

    # Poll until done
    while True:
        desc = redshift_data.describe_statement(Id=statement_id)
        if desc["Status"] == "FINISHED":
            break
        if desc["Status"] in ("FAILED", "ABORTED"):
            raise ValueError("Redshift: " + desc.get("Error", desc["Status"]))
        time.sleep(0.5)

    # Read results for single statements that return rows
    if len(sqls) > 1 or not desc.get("HasResultSet"):
        return [], []
    result = redshift_data.get_statement_result(Id=statement_id)
    names = [column["name"] for column in result["ColumnMetadata"]]
    rows = []
    # Each field is {"stringValue"|"longValue"|"doubleValue"|"booleanValue": value} or {"isNull": true}
    for record in result["Records"]:
        rows.append([None if field.get("isNull") else next(iter(field.values())) for field in record])
    return names, rows


# Turn a Python value into a SQL literal
def sql_literal(value) -> str:
    """
    Format a generated value as a SQL literal. Strings are escaped by doubling single quotes.

    Arguments:
        value: None, bool, int, float or str

    Returns:
        str: SQL literal
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


# SQL statements that (re)create a schema-qualified table
def recreate_sql(dest: dict, columns: list) -> list:
    """
    Build the statements that create the schema if missing and replace the table.
    Names are pre-validated, then quoted.

    Arguments:
        dest (dict): validated destination
        columns (list): validated column dicts

    Returns:
        list: SQL statements
    """
    ddl = ", ".join(f'"{c["name"]}" {TYPES[c["type"]]["sql"]}' for c in columns)
    qualified = f'"{dest["schema"]}"."{dest["table"]}"'
    return [
        f'CREATE SCHEMA IF NOT EXISTS "{dest["schema"]}"',
        f"DROP TABLE IF EXISTS {qualified}",
        f"CREATE TABLE {qualified} ({ddl})",
    ]


# =================================================
# S3 HELPERS
# =================================================


# Folder of a dataset inside its bucket
def dataset_folder(dest: dict) -> str:
    """
    Build the key prefix of a dataset: <prefix>/<table>/.

    Arguments:
        dest (dict): validated destination

    Returns:
        str: key prefix ending with /
    """
    return f"{dest['prefix']}/{dest['table']}/" if dest["prefix"] else f"{dest['table']}/"


# Delete every object under a prefix
def delete_prefix(bucket: str, prefix: str) -> None:
    """
    Delete all objects under a prefix (use "" for the whole bucket).

    Arguments:
        bucket (str): bucket name
        prefix (str): key prefix
    """
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


# Delete a dataset's part files (not nested datasets that share the folder name)
def delete_dataset(bucket: str, folder: str) -> None:
    """
    Delete the part files directly inside a dataset folder.

    Arguments:
        bucket (str): bucket name
        folder (str): dataset key prefix ending with /
    """
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=folder, Delimiter="/"):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


# Whether a bucket exists
def bucket_exists(bucket: str) -> bool:
    """
    Check whether a bucket exists.

    Arguments:
        bucket (str): full bucket name

    Returns:
        bool: True if it exists
    """
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            return False
        raise


# Write a dataset's part files, replacing the old ones
def write_dataset(dest: dict, columns: list, batches, hive_csv: bool) -> tuple:
    """
    Write one part file per batch to s3://<bucket>/<prefix>/<table>/, creating the bucket if needed.

    Arguments:
        dest (dict): validated destination
        columns (list): validated column dicts
        batches: iterable of row lists
        hive_csv (bool): write CSV for Athena (see formats.encode)

    Returns:
        tuple: (bucket, folder, number of part files)
    """
    bucket = BUCKET_PREFIX + dest["bucket"]

    # Create the bucket the first time it is used
    if not bucket_exists(bucket):
        if REGION == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})

    # Replace old part files with one new file per batch
    folder = dataset_folder(dest)
    delete_dataset(bucket, folder)
    parts = 0
    for rows in batches:
        body = formats.encode(dest["format"], columns, rows, hive_csv)
        s3.put_object(Bucket=bucket, Key=f"{folder}part-{parts:05d}.{dest['format']}", Body=body)
        parts += 1
    return bucket, folder, parts


# =================================================
# WRITE
# =================================================


# Write generated batches to a destination, replacing the table if it exists
def write(dest: dict, columns: list, batches) -> dict:
    """
    Create the destination if needed and write every batch.

    Arguments:
        dest (dict): validated destination (see app.check_destination)
        columns (list): validated column dicts
        batches: iterable of row lists

    Returns:
        dict: where the data went. For SQL sources the caller adds connection details
    """
    source = dest["source"]

    # Postgres: one transaction, multi-row INSERTs per batch
    if source == "postgres":
        con = pg_connect(dest["db"])
        try:
            con.run("BEGIN")
            for sql in recreate_sql(dest, columns):
                con.run(sql)
            names = ", ".join(f'"{c["name"]}"' for c in columns)
            prefix = f'INSERT INTO "{dest["schema"]}"."{dest["table"]}" ({names}) VALUES '
            for rows in batches:
                chunk = []
                size = 0
                for row in rows:
                    values = "(" + ", ".join(sql_literal(row[c["name"]]) for c in columns) + ")"
                    if chunk and size + len(values) > PG_STATEMENT_CHARS:
                        con.run(prefix + ", ".join(chunk))
                        chunk = []
                        size = 0
                    chunk.append(values)
                    size += len(values) + 2
                if chunk:
                    con.run(prefix + ", ".join(chunk))
            con.run("COMMIT")
        except pg8000.native.DatabaseError as e:
            raise ValueError(f"Postgres: {e}")
        finally:
            con.close()
        return {"table": f"{dest['schema']}.{dest['table']}"}

    # Redshift: stage one JSON file per batch, then replace the table and COPY them all in one transaction
    if source == "redshift":
        staging = f"redshift/{uuid.uuid4()}/"
        try:
            for i, rows in enumerate(batches):
                body = "\n".join(json.dumps(row) for row in rows)
                s3.put_object(Bucket=STAGING_BUCKET, Key=f"{staging}part-{i:05d}.json", Body=body.encode("utf-8"))
            copy = (
                f'COPY "{dest["schema"]}"."{dest["table"]}" FROM \'s3://{STAGING_BUCKET}/{staging}\' '
                f"IAM_ROLE '{REDSHIFT_ROLE_ARN}' FORMAT AS JSON 'auto' DATEFORMAT 'auto' TIMEFORMAT 'auto'"
            )
            rs_exec(dest["db"], recreate_sql(dest, columns) + [copy])
        finally:
            delete_prefix(STAGING_BUCKET, staging)
        return {"table": f"{dest['schema']}.{dest['table']}"}

    # DynamoDB: recreate the table, then write each batch. Every item gets a uuid "pk"
    if source == "dynamodb":
        name = DYNAMO_PREFIX + dest["table"]
        wait = {"Delay": 2, "MaxAttempts": 90}
        try:
            dynamo.delete_table(TableName=name)
            dynamo.get_waiter("table_not_exists").wait(TableName=name, WaiterConfig=wait)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        dynamo.create_table(
            TableName=name,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamo.get_waiter("table_exists").wait(TableName=name, WaiterConfig=wait)
        table = dynamo_resource.Table(name)
        for rows in batches:
            with table.batch_writer() as writer:
                for row in rows:
                    item = {"pk": str(uuid.uuid4())}
                    # DynamoDB needs Decimal instead of float
                    for key, value in row.items():
                        item[key] = Decimal(str(value)) if isinstance(value, float) else value
                    writer.put_item(Item=item)
        arn = dynamo.describe_table(TableName=name)["Table"]["TableArn"]
        return {"type": "dynamodb", "table": name, "region": REGION, "arn": arn, "partition_key": "pk"}

    # S3: part files in the chosen format
    if source == "s3":
        bucket, folder, parts = write_dataset(dest, columns, batches, False)
        return {"type": "s3", "uri": f"s3://{bucket}/{folder}", "format": dest["format"], "part_files": parts, "region": REGION}

    # Glue: part files in S3 plus a catalog table over them (Athena-readable)
    database = GLUE_PREFIX + dest["database"]
    bucket, folder, parts = write_dataset(dest, columns, batches, True)
    try:
        glue.create_database(DatabaseInput={"Name": database})
    except ClientError as e:
        if e.response["Error"]["Code"] != "AlreadyExistsException":
            raise
    try:
        glue.delete_table(DatabaseName=database, Name=dest["table"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
    fmt = GLUE_FORMATS[dest["format"]]
    params = {"classification": dest["format"]}
    if dest["format"] == "csv":
        params["skip.header.line.count"] = "1"
    if dest["format"] == "avro":
        params["avro.schema.literal"] = json.dumps(formats.avro_schema(columns))
    glue.create_table(
        DatabaseName=database,
        TableInput={
            "Name": dest["table"],
            "TableType": "EXTERNAL_TABLE",
            "Parameters": params,
            "StorageDescriptor": {
                "Columns": [{"Name": c["name"], "Type": TYPES[c["type"]]["glue"]} for c in columns],
                "Location": f"s3://{bucket}/{folder}",
                "InputFormat": fmt["input"],
                "OutputFormat": fmt["output"],
                "SerdeInfo": {"SerializationLibrary": fmt["serde"], "Parameters": fmt["params"]},
            },
        },
    )
    return {
        "type": "glue",
        "database": database,
        "table": dest["table"],
        "location": f"s3://{bucket}/{folder}",
        "format": dest["format"],
        "part_files": parts,
        "region": REGION,
        "athena_query": f'SELECT * FROM "{database}"."{dest["table"]}" LIMIT 10;',
    }


# =================================================
# BROWSING
# =================================================


# List the mock data buckets
def list_buckets() -> list:
    """
    List buckets created by this tool.

    Returns:
        list: short bucket names (without BUCKET_PREFIX)
    """
    names = []
    for bucket in s3.list_buckets()["Buckets"]:
        if bucket["Name"].startswith(BUCKET_PREFIX):
            names.append(bucket["Name"][len(BUCKET_PREFIX) :])
    return sorted(names)


# List the mock data Glue databases
def list_glue_databases() -> list:
    """
    List Glue databases created by this tool.

    Returns:
        list: short database names (without GLUE_PREFIX)
    """
    names = []
    for page in glue.get_paginator("get_databases").paginate():
        for db in page["DatabaseList"]:
            if db["Name"].startswith(GLUE_PREFIX):
                names.append(db["Name"][len(GLUE_PREFIX) :])
    return sorted(names)


# What already exists at a location, for the dropdowns
def browse(dest: dict) -> dict:
    """
    List existing schemas/tables/datasets at a partial destination.

    Arguments:
        dest (dict): validated destination without a table

    Returns:
        dict: {"schemas": {schema: [tables]}} for SQL, {"datasets": {prefix: [tables]}} for s3,
              {"tables": [names]} for dynamodb and glue
    """
    source = dest["source"]

    # Postgres / Redshift: every schema and its tables
    if source in ("postgres", "redshift"):
        rows = pg_run(dest["db"], [SCHEMAS_SQL]) if source == "postgres" else rs_exec(dest["db"], [SCHEMAS_SQL])[1]
        schemas = {}
        for schema, table in rows:
            schemas.setdefault(schema, [])
            if table:
                schemas[schema].append(table)
        return {"schemas": schemas}

    # DynamoDB tables made by this tool
    if source == "dynamodb":
        tables = []
        for page in dynamo.get_paginator("list_tables").paginate():
            for name in page["TableNames"]:
                if name.startswith(DYNAMO_PREFIX):
                    tables.append(name[len(DYNAMO_PREFIX) :])
        return {"tables": tables}

    # S3: datasets grouped by prefix, found from their part files
    if source == "s3":
        datasets = {}
        bucket = BUCKET_PREFIX + dest["bucket"]
        if bucket_exists(bucket):
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                for obj in page.get("Contents", []):
                    match = PART_RE.match(obj["Key"])
                    if match:
                        tables = datasets.setdefault(match.group(1) or "", [])
                        if match.group(2) not in tables:
                            tables.append(match.group(2))
        return {"datasets": datasets}

    # Glue: tables in the database
    tables = []
    try:
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=GLUE_PREFIX + dest["database"]):
            for table in page["TableList"]:
                tables.append(table["Name"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityNotFoundException":
            raise
    return {"tables": tables}


# =================================================
# DELETING
# =================================================


# Delete a table or dataset
def delete_table(dest: dict) -> None:
    """
    Delete a table. For s3 this deletes the dataset's part files; for glue the catalog table and its files.

    Arguments:
        dest (dict): validated destination with a table
    """
    source = dest["source"]
    qualified = f'"{dest.get("schema")}"."{dest["table"]}"'
    if source == "postgres":
        pg_run(dest["db"], [f"DROP TABLE IF EXISTS {qualified}"])
    elif source == "redshift":
        rs_exec(dest["db"], [f"DROP TABLE IF EXISTS {qualified}"])
    elif source == "dynamodb":
        dynamo.delete_table(TableName=DYNAMO_PREFIX + dest["table"])
    elif source == "s3":
        delete_dataset(BUCKET_PREFIX + dest["bucket"], dataset_folder(dest))
    elif source == "glue":
        database = GLUE_PREFIX + dest["database"]
        location = glue.get_table(DatabaseName=database, Name=dest["table"])["Table"]["StorageDescriptor"]["Location"]
        # Only delete files that live in one of this tool's buckets
        if location.startswith("s3://" + BUCKET_PREFIX):
            bucket, _, folder = location[len("s3://") :].partition("/")
            delete_dataset(bucket, folder)
        glue.delete_table(DatabaseName=database, Name=dest["table"])


# Delete a mock data bucket and everything in it
def delete_bucket(bucket_name: str) -> None:
    """
    Empty and delete a bucket created by this tool.

    Arguments:
        bucket_name (str): validated short bucket name
    """
    bucket = BUCKET_PREFIX + bucket_name
    delete_prefix(bucket, "")
    s3.delete_bucket(Bucket=bucket)
