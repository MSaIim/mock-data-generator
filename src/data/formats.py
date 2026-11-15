import csv
import io
import json
from datetime import date, datetime
from itertools import islice

import fastavro
import pyarrow as pa
import pyarrow.parquet as pq

from data.generator import TYPES

# =================================================
# FILE FORMATS FOR S3 / GLUE DATASETS
# =================================================

FORMATS = ("csv", "json", "parquet", "avro")

# Formats with a fixed schema can't hold values of the wrong type
TYPED_FORMATS = ("parquet", "avro")

ARROW_TYPES = {
    "string": pa.string(),
    "bigint": pa.int64(),
    "double": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "timestamp": pa.timestamp("ms"),
}

AVRO_TYPES = {
    "string": "string",
    "bigint": "long",
    "double": "double",
    "boolean": "boolean",
    "date": {"type": "int", "logicalType": "date"},
    "timestamp": {"type": "long", "logicalType": "timestamp-millis"},
}


# Convert a generated value to the Python type a typed format expects
def typed_value(value, kind: str):
    """
    Convert date and timestamp strings to date/datetime objects. Other values pass through.

    Arguments:
        value: generated value (already type-correct for typed formats)
        kind (str): Glue type of the column

    Returns:
        the converted value
    """
    if value is None:
        return None
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "timestamp":
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return value


# Avro schema for the columns
def avro_schema(columns: list) -> dict:
    """
    Build an Avro record schema where every field is nullable.

    Arguments:
        columns (list): validated column dicts

    Returns:
        dict: Avro schema
    """
    fields = []
    for c in columns:
        fields.append({"name": c["name"], "type": ["null", AVRO_TYPES[TYPES[c["type"]]["glue"]]], "default": None})
    return {"type": "record", "name": "row", "fields": fields}


# Encode rows as one file
def encode(fmt: str, columns: list, rows: list, hive_csv: bool) -> bytes:
    """
    Encode rows in the given file format.

    Arguments:
        fmt (str): csv, json, parquet or avro
        columns (list): validated column dicts
        rows (list): generated rows
        hive_csv (bool): for Glue tables, write CSV that Athena's LazySimpleSerDe reads with types:
            no quoting, delimiters escaped with a backslash, nulls as empty fields

    Returns:
        bytes: file contents
    """
    names = [c["name"] for c in columns]

    # CSV with a header row
    if fmt == "csv":
        out = io.StringIO()
        if hive_csv:
            writer = csv.writer(out, quoting=csv.QUOTE_NONE, escapechar="\\", lineterminator="\n")
        else:
            writer = csv.writer(out, lineterminator="\n")
        writer.writerow(names)
        for row in rows:
            cells = []
            for name in names:
                value = row[name]
                if hive_csv and isinstance(value, bool):
                    value = "true" if value else "false"
                elif hive_csv and isinstance(value, str):
                    value = value.replace("\n", " ").replace("\r", " ")
                cells.append(value)
            writer.writerow(cells)
        return out.getvalue().encode("utf-8")

    # JSON lines
    if fmt == "json":
        return ("\n".join(json.dumps(row) for row in rows) + "\n").encode("utf-8")

    # Parquet with real column types
    if fmt == "parquet":
        arrays = []
        for c in columns:
            kind = TYPES[c["type"]]["glue"]
            arrays.append(pa.array([typed_value(row[c["name"]], kind) for row in rows], type=ARROW_TYPES[kind]))
        buffer = pa.BufferOutputStream()
        pq.write_table(pa.Table.from_arrays(arrays, names=names), buffer)
        return buffer.getvalue().to_pybytes()

    # Avro with real column types
    records = []
    for row in rows:
        record = {}
        for c in columns:
            record[c["name"]] = typed_value(row[c["name"]], TYPES[c["type"]]["glue"])
        records.append(record)
    out = io.BytesIO()
    fastavro.writer(out, fastavro.parse_schema(avro_schema(columns)), records)
    return out.getvalue()


# Read the first rows back out of a file
def decode(fmt: str, data: bytes, hive_csv: bool, limit: int) -> tuple:
    """
    Decode the first rows of a file written by encode (used for dataset previews).

    Arguments:
        fmt (str): csv, json, parquet or avro
        data (bytes): file contents
        hive_csv (bool): the CSV was written for Athena (backslash-escaped, unquoted)
        limit (int): maximum rows to return

    Returns:
        tuple: (column names, list of row dicts, {name: type} from the file's schema; empty for CSV/JSON)
    """
    # CSV: header row, empty fields are nulls
    if fmt == "csv":
        text = io.StringIO(data.decode("utf-8"))
        reader = csv.reader(text, quoting=csv.QUOTE_NONE, escapechar="\\") if hive_csv else csv.reader(text)
        names = next(reader, [])
        rows = []
        for values in islice(reader, limit):
            rows.append({name: (None if value == "" else value) for name, value in zip(names, values)})
        return names, rows, {}

    # JSON lines
    if fmt == "json":
        rows = [json.loads(line) for line in islice(data.decode("utf-8").splitlines(), limit) if line]
        return (list(rows[0]) if rows else []), rows, {}

    # Parquet: types from the file schema
    if fmt == "parquet":
        table = pq.read_table(io.BytesIO(data))
        types = {field.name: str(field.type) for field in table.schema}
        return table.column_names, table.slice(0, limit).to_pylist(), types

    # Avro: types from the writer schema (skip the "null" in nullable unions)
    reader = fastavro.reader(io.BytesIO(data))
    rows = list(islice(reader, limit))
    names = []
    types = {}
    for field in reader.writer_schema["fields"]:
        names.append(field["name"])
        kind = field["type"]
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "null")
        if isinstance(kind, dict):
            kind = kind.get("logicalType") or kind.get("type")
        types[field["name"]] = str(kind)
    return names, rows, types
