import random

from faker import Faker

# =================================================
# COLUMN TYPES
# =================================================

# sql: column type in Postgres/Redshift, glue: column type in the Glue catalog,
# make: clean value, junk: values that break the pattern but still fit the SQL column type
TYPES = {
    "string": {
        "sql": "VARCHAR(256)",
        "glue": "string",
        "make": lambda f: f.word(),
        "junk": ["", "   ", "N/A", "???", "null"],
    },
    "text": {
        "sql": "VARCHAR(1024)",
        "glue": "string",
        "make": lambda f: f.sentence(),
        "junk": ["", "   ", "N/A", "lorem ipsum " * 20],
    },
    "integer": {
        "sql": "BIGINT",
        "glue": "bigint",
        "make": lambda f: f.random_int(0, 100000),
        "junk": [-1, 0, -999999, 9999999999],
    },
    "float": {
        "sql": "DOUBLE PRECISION",
        "glue": "double",
        "make": lambda f: round(f.pyfloat(min_value=0, max_value=10000), 2),
        "junk": [-1.0, 0.0, -99999.99, 99999999.99],
    },
    "boolean": {
        "sql": "BOOLEAN",
        "glue": "boolean",
        "make": lambda f: f.pybool(),
        "junk": [],
    },
    "date": {
        "sql": "DATE",
        "glue": "date",
        "make": lambda f: f.date_between("-5y", "today").isoformat(),
        "junk": ["1900-01-01", "1970-01-01", "2999-12-31"],
    },
    "timestamp": {
        "sql": "TIMESTAMP",
        "glue": "timestamp",
        "make": lambda f: f.date_time_between("-5y", "now").strftime("%Y-%m-%d %H:%M:%S"),
        "junk": ["1900-01-01 00:00:00", "1970-01-01 00:00:00", "2999-12-31 23:59:59"],
    },
    "uuid": {
        "sql": "VARCHAR(36)",
        "glue": "string",
        "make": lambda f: f.uuid4(),
        "junk": ["00000000-0000-0000-0000-000000000000", "not-a-uuid", "1234"],
    },
    "name": {
        "sql": "VARCHAR(256)",
        "glue": "string",
        "make": lambda f: f.name(),
        "junk": ["", "   ", "1234", "N/A", "x"],
    },
    "email": {
        "sql": "VARCHAR(256)",
        "glue": "string",
        "make": lambda f: f.email(),
        "junk": ["john.doe", "@example.com", "john@@example", "john doe@example.com", ""],
    },
    "phone": {
        "sql": "VARCHAR(64)",
        "glue": "string",
        "make": lambda f: f.phone_number(),
        "junk": ["123", "phone", "000-000-0000", "555-CALL-NOW", ""],
    },
    "address": {
        "sql": "VARCHAR(512)",
        "glue": "string",
        "make": lambda f: f.address().replace("\n", ", "),
        "junk": ["", "N/A", "123", "unknown"],
    },
    "city": {
        "sql": "VARCHAR(256)",
        "glue": "string",
        "make": lambda f: f.city(),
        "junk": ["", "N/A", "12345"],
    },
    "country": {
        "sql": "VARCHAR(256)",
        "glue": "string",
        "make": lambda f: f.country(),
        "junk": ["", "N/A", "XX", "Narnia"],
    },
    "company": {
        "sql": "VARCHAR(256)",
        "glue": "string",
        "make": lambda f: f.company(),
        "junk": ["", "N/A", "-"],
    },
    "url": {
        "sql": "VARCHAR(512)",
        "glue": "string",
        "make": lambda f: f.url(),
        "junk": ["htp:/broken", "example", "www.", ""],
    },
    "ipv4": {
        "sql": "VARCHAR(64)",
        "glue": "string",
        "make": lambda f: f.ipv4(),
        "junk": ["999.999.999.999", "1.2.3", "localhost", ""],
    },
}

# Values of the wrong type entirely. Only used for stores without a fixed schema (DynamoDB, S3, Glue)
WRONG_TYPE_JUNK = {
    "integer": ["abc", "12.5.3", "1,000", 3.14],
    "float": ["ten", "1,234.56", "--1", True],
    "boolean": ["yes", "maybe", 2, "0"],
    "date": ["31/02/2023", "not-a-date", 20230101],
    "timestamp": ["yesterday", "2023-13-45 25:61:00", 1700000000],
}

# Wrong-type junk for every text-like type
STRING_WRONG_TYPE_JUNK = [12345, 3.14, True]

# =================================================
# GENERATION
# =================================================


# Generate mock rows for the given column definitions
def generate_rows(columns: list, row_count: int, allow_wrong_types: bool) -> list:
    """
    Generate rows of fake data. Each column can have a share of null values and junk values.

    Arguments:
        columns (list): column dicts with name, type, null_pct and junk_pct (already validated)
        row_count (int): number of rows to generate
        allow_wrong_types (bool): whether junk may use values of the wrong type

    Returns:
        list: one dict per row, keyed by column name
    """
    fake = Faker()
    rows = []
    for _ in range(row_count):
        row = {}
        for col in columns:
            kind = col["type"]
            roll = random.random() * 100
            # Null values
            if roll < col["null_pct"]:
                row[col["name"]] = None
            # Junk values
            elif roll < col["null_pct"] + col["junk_pct"]:
                pool = TYPES[kind]["junk"]
                if allow_wrong_types:
                    pool = pool + WRONG_TYPE_JUNK.get(kind, STRING_WRONG_TYPE_JUNK)
                # Boolean columns in SQL have no pattern to break, so their junk is null
                row[col["name"]] = random.choice(pool) if pool else None
            # Clean values
            else:
                row[col["name"]] = TYPES[kind]["make"](fake)
        rows.append(row)
    return rows
