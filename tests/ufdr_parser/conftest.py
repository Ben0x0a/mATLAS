"""Shared fixtures for ufdr_parser tests: a tiny synthetic Cellebrite report.

Defines:    REPORT_XML (a minimal report mirroring the real structure) and the
            ``report_xml`` / ``report_ufdr`` fixtures (bare .xml and STORED-zip forms).
Used by:    the ufdr_parser test modules.
Depends on: pytest, standard library zipfile.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ufdr_parser.const import REPORT_ENTRY_NAME

# A minimal report: a taggedFiles stub, two modelTypes (one with a 1:1 modelField,
# one with a 1:many multiModelField), and a matching extraInfos id->source block.
REPORT_XML = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://pa.cellebrite.com/report/2.0" name="synthetic">
  <taggedFiles>
    <file id="f1" path="/a/b.jpg" name="b.jpg" source_index="2">
      <accessInfo/>
    </file>
  </taggedFiles>
  <decodedData>
    <modelType type="Location">
      <model type="Location" id="loc1" deleted_state="Intact">
        <field name="Source" type="String"><value type="String"><![CDATA[Apple Maps]]></value></field>
        <modelField name="Position" type="Coordinate">
          <model type="Coordinate" id="coord1">
            <field name="Latitude" type="Double"><value type="Double"><![CDATA[38.34]]></value></field>
            <field name="Longitude" type="Double"><value type="Double"><![CDATA[-0.47]]></value></field>
          </model>
        </modelField>
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp" format="TimeStampKnown" formattedTimestamp="2025-12-05T10:19:15+00:00">2025-12-05T10:19:15.073+00:00</value></field>
        <field name="Name" type="String"><empty/></field>
      </model>
    </modelType>
    <modelType type="Call">
      <model type="Call" id="call1" deleted_state="Intact">
        <field name="Direction" type="ModelDirections"><value type="ModelDirections"><![CDATA[Outgoing]]></value></field>
        <multiModelField name="Parties" type="Party">
          <model type="Party" id="party1">
            <field name="Identifier" type="String"><value type="String"><![CDATA[+34123]]></value></field>
            <field name="Role" type="PartyRole"><value type="PartyRole"><![CDATA[To]]></value></field>
          </model>
        </multiModelField>
      </model>
    </modelType>
  </decodedData>
  <extraInfos>
    <extraInfo type="model" id="loc1"><sourceInfo><nodeInfos><nodeInfo name="Cache.sqlite" path="files_full.zip/private/Cache.sqlite" tableName="ZRTCLLOCATION" offset="123" size="456"/></nodeInfos></sourceInfo></extraInfo>
    <extraInfo type="model" id="call1"><sourceInfo><nodeInfos><nodeInfo name="CallHistory.storedata-wal" path="files_full.zip/private/CallHistory.storedata-wal" tableName="ZCALLRECORD" offset="1159850" size="1207192"/></nodeInfos></sourceInfo></extraInfo>
  </extraInfos>
</project>
"""


@pytest.fixture
def report_xml(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic_report.xml"
    path.write_text(REPORT_XML, encoding="utf-8")
    return path


@pytest.fixture
def report_ufdr(tmp_path: Path) -> Path:
    """The same report wrapped in a STORED zip, like a real .ufdr."""
    path = tmp_path / "synthetic_report.ufdr"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(REPORT_ENTRY_NAME, REPORT_XML)
    return path
