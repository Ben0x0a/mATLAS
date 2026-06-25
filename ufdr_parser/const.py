"""Format constants for the Cellebrite UFDR report (``report.xml``).

Defines:    the report namespace, the entry name inside the .ufdr zip, the fully
            qualified tag names the streaming reader matches, and the CSV id/source
            column names.
Used by:    archive, reader, models, source_lookup, writer.
Depends on: standard library only.

These are FORMAT constants (fixed by Cellebrite's report schema), not operator-tunable
settings, so they live here rather than in a user config file.
"""
from __future__ import annotations

# Cellebrite UFED Physical Analyzer report namespace (report schema 2.0).
NS = "http://pa.cellebrite.com/report/2.0"

# The single decoded-report entry inside a .ufdr archive.
REPORT_ENTRY_NAME = "report.xml"

# Fully qualified tag names (namespace-clarked) the reader dispatches on.
TAG_MODEL = f"{{{NS}}}model"
TAG_MODEL_TYPE = f"{{{NS}}}modelType"
TAG_FIELD = f"{{{NS}}}field"
TAG_VALUE = f"{{{NS}}}value"
TAG_MODEL_FIELD = f"{{{NS}}}modelField"
TAG_MULTI_MODEL_FIELD = f"{{{NS}}}multiModelField"
TAG_FILE = f"{{{NS}}}file"
TAG_EXTRA_INFO = f"{{{NS}}}extraInfo"
TAG_NODE_INFO = f"{{{NS}}}nodeInfo"

# Local names (namespace stripped) for the same tags, for parent/child checks.
LOCAL_MODEL = "model"
LOCAL_MODEL_TYPE = "modelType"
LOCAL_MODEL_FIELD = "modelField"
LOCAL_MULTI_MODEL_FIELD = "multiModelField"

# Record levels: a top-level model, a 1:1/1:many child, and a grandchild.
LEVEL_TOP = 0
LEVEL_SUB = 1
LEVEL_SUBSUB = 2

# CSV filename suffixes per level (mirrors the legacy UFEDParser naming).
LEVEL_FILE_PREFIX = {LEVEL_TOP: "", LEVEL_SUB: "SM", LEVEL_SUBSUB: "SSM"}

# Identity columns written ahead of a record's own fields.
COL_UUID = "uuid"            # top-level model id
COL_SUB_UUID = "sub-uuid"    # child/grandchild model id
COL_MAIN_UUID = "main-uuid"  # immediate parent model id

# Source-provenance columns appended after a record's own fields, populated by the
# <extraInfos> id->source lookup at flush time.
COL_SOURCE_PATH = "source_path"
COL_SOURCE_NAME = "source_name"
COL_SOURCE_TABLE = "source_table"
COL_SOURCE_OFFSET = "source_offset"
COL_SOURCE_SIZE = "source_size"
SOURCE_COLUMNS = (
    COL_SOURCE_PATH,
    COL_SOURCE_NAME,
    COL_SOURCE_TABLE,
    COL_SOURCE_OFFSET,
    COL_SOURCE_SIZE,
)
