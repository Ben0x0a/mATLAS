"""Tests for the post-dump brick runner and the combined-workbook brick.

Defines:    a synthetic report covering chats (stated / inferred / not-inferable recipients),
            a standalone message, a call, an email with Cc + attachment, a contact and a
            location, and tests over the resulting <base>_combined.xlsx.
Used by:    pytest.
Depends on: ufdr_parser (dump, bricks), openpyxl.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from ufdr_parser.bricks import base as bricks_base
from ufdr_parser.bricks.workbook import xlsx
from ufdr_parser.dump import run_dump

NS = "http://pa.cellebrite.com/report/2.0"


def _party(pid: str, ident: str, role: str, name: str = "", owner: str = "") -> str:
    name_xml = f'<field name="Name" type="String"><value type="String"><![CDATA[{name}]]></value></field>' if name else ""
    owner_xml = f'<field name="IsPhoneOwner" type="Boolean"><value type="Boolean"><![CDATA[{owner}]]></value></field>' if owner else ""
    ident_xml = f'<field name="Identifier" type="String"><value type="String"><![CDATA[{ident}]]></value></field>' if ident else ""
    return (
        f'<model type="Party" id="{pid}">{ident_xml}'
        f'<field name="Role" type="PartyRole"><value type="PartyRole"><![CDATA[{role}]]></value></field>'
        f"{name_xml}{owner_xml}</model>"
    )


def _user_id(uid: str, category: str, value: str) -> str:
    return (
        f'<model type="UserID" id="{uid}">'
        f'<field name="Category" type="String"><value type="String"><![CDATA[{category}]]></value></field>'
        f'<field name="Value" type="String"><value type="String"><![CDATA[{value}]]></value></field>'
        "</model>"
    )


def _coordinate(cid: str, lat: str, lon: str) -> str:
    return (
        f'<model type="Coordinate" id="{cid}">'
        f'<field name="Latitude" type="Double"><value type="Double"><![CDATA[{lat}]]></value></field>'
        f'<field name="Longitude" type="Double"><value type="Double"><![CDATA[{lon}]]></value></field>'
        "</model>"
    )


def _message(mid: str, ts: str, body: str, parties: str) -> str:
    return (
        f'<model type="InstantMessage" id="{mid}">'
        f'<field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">{ts}</value></field>'
        f'<field name="Body" type="String"><value type="String"><![CDATA[{body}]]></value></field>'
        f'<multiModelField name="From" type="Party">{parties}</multiModelField>'
        "</model>"
    )


REPORT = f"""<?xml version="1.0" encoding="utf-8"?>
<project xmlns="{NS}" name="wb" reportVersion="8.5" extractionType="FileSystem">
  <sourceExtractions>
    <extractionInfo id="0" name="File System" type="FileSystem" deviceName="IPHONE" index="0" />
  </sourceExtractions>
  <caseInformation>
    <field name="Case name" fieldType="CaseName">Test case</field>
  </caseInformation>
  <metadata section="Extraction Data">
    <item name="DeviceInfoExtractionStartDateTime" sourceExtraction="0"><![CDATA[03/09/2025 17:35:59(UTC+2)]]></item>
  </metadata>
  <images>
    <image key="FileDump" path="EXTRACTION_FFS.zip" size="10">
      <metadata section="Hashes"><item name="SHA256"><![CDATA[ABCD]]></item></metadata>
    </image>
  </images>
  <taggedFiles />
  <decodedData>
    <modelType type="Chat">
      <model type="Chat" id="chat1">
        <field name="Source" type="String"><value type="String"><![CDATA[WhatsApp]]></value></field>
        <field name="Name" type="String"><value type="String"><![CDATA[Famille]]></value></field>
        <multiModelField name="Participants" type="Party">
          {_party("cp1", "+41790000001", "General", "Alice", "True")}
          {_party("cp2", "+41790000002", "General", "Bob")}
        </multiModelField>
        <multiModelField name="Messages" type="InstantMessage">
          {_message("m2", "2025-01-01T10:05:00.000+00:00", "=SUM(1,2)", _party("p2", "+41790000002", "From", "Bob") + _party("p3", "+41790000001", "To", "Alice", "True"))}
          {_message("m1", "2025-01-01T10:00:00.000+00:00", "Salut, ça va ?", _party("p1", "+41790000001", "From", "Alice", "True") + _party("pe", "", "To"))}
          {_message("m3", "2025-01-01T10:10:00.000+00:00", "stranger", _party("p4", "+41799999999", "From"))}
        </multiModelField>
      </model>
    </modelType>
    <modelType type="InstantMessage">
      {_message("im1", "2025-01-02T08:00:00.000+00:00", "SMS seul", _party("p5", "+41790000003", "From"))}
    </modelType>
    <modelType type="Call">
      <model type="Call" id="call1">
        <field name="Direction" type="String"><value type="String"><![CDATA[Outgoing]]></value></field>
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-03T09:00:00.000+00:00</value></field>
        <field name="Duration" type="TimeSpan"><value type="TimeSpan"><![CDATA[00:01:00]]></value></field>
        <multiModelField name="Parties" type="Party">
          {_party("c1", "+41790000001", "From", "Alice", "True")}
          {_party("c2", "+41790000002", "To", "Bob")}
        </multiModelField>
      </model>
    </modelType>
    <modelType type="Email">
      <model type="Email" id="mail1">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-04T12:00:00.000</value></field>
        <field name="Subject" type="String"><value type="String"><![CDATA[Réunion]]></value></field>
        <multiModelField name="Parties" type="Party">
          {_party("e1", "a@x.ch", "From")}
          {_party("e2", "b@x.ch", "To")}
          {_party("e3", "c@x.ch", "Cc")}
        </multiModelField>
        <multiModelField name="Attachments" type="Attachment">
          <model type="Attachment" id="att1">
            <field name="Filename" type="String"><value type="String"><![CDATA[plan.pdf]]></value></field>
            <field name="attachment_extracted_path" type="String"><value type="String"><![CDATA[files/plan.pdf]]></value></field>
          </model>
        </multiModelField>
      </model>
    </modelType>
    <modelType type="Contact">
      <model type="Contact" id="ct1">
        <field name="Source" type="String"><value type="String"><![CDATA[WhatsApp]]></value></field>
        <field name="Name" type="String"><value type="String"><![CDATA[Bob]]></value></field>
        <multiModelField name="Entries" type="PhoneNumber">
          <model type="PhoneNumber" id="ph1">
            <field name="Category" type="String"><value type="String"><![CDATA[Mobile]]></value></field>
            <field name="Value" type="String"><value type="String"><![CDATA[+41790000002]]></value></field>
          </model>
        </multiModelField>
        <multiModelField name="Ids" type="UserID">
          {_user_id("u1", "WhatsApp User Id", "41790000002@s.whatsapp.net")}
          {_user_id("u2", "Push Name", "Bobby")}
          {_user_id("u3", "Instagram Id", "bob.insta")}
        </multiModelField>
      </model>
      <model type="Contact" id="ct2">
        <field name="Source" type="String"><value type="String"><![CDATA[InteractionC]]></value></field>
        <multiModelField name="Ids" type="UserID">
          {_user_id("u4", "Identifier", "abc-123")}
        </multiModelField>
      </model>
    </modelType>
    <modelType type="Location">
      <model type="Location" id="loc1">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-05T07:00:00.000+00:00</value></field>
        <modelField name="Position" type="Coordinate">
          <model type="Coordinate" id="co1">
            <field name="Latitude" type="Double"><value type="Double"><![CDATA[46.5]]></value></field>
            <field name="Longitude" type="Double"><value type="Double"><![CDATA[6.6]]></value></field>
          </model>
        </modelField>
      </model>
    </modelType>
    <modelType type="DeviceInfoEntry">
      <model type="DeviceInfoEntry" id="dev1">
        <field name="EntryName" type="String"><value type="String"><![CDATA[IMEI]]></value></field>
        <field name="EntryValue" type="String"><value type="String"><![CDATA[353872860051601]]></value></field>
        <field name="EntryCategory" type="String"><value type="String"><![CDATA[General]]></value></field>
        <field name="Source" type="String"><value type="String"><![CDATA[Native]]></value></field>
      </model>
    </modelType>
    <modelType type="CellTower">
      <model type="CellTower" id="cell1">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-05T06:00:00.000+00:00</value></field>
        <field name="MCC" type="String"><value type="String"><![CDATA[228]]></value></field>
        <field name="CID" type="String"><value type="String"><![CDATA[42]]></value></field>
        <modelField name="Position" type="Coordinate">{_coordinate("cc1", "46.1", "6.1")}</modelField>
      </model>
    </modelType>
    <modelType type="WirelessNetwork">
      <model type="WirelessNetwork" id="wifi1">
        <field name="SSId" type="String"><value type="String"><![CDATA[Home]]></value></field>
        <field name="LastConnection" type="TimeStamp"><value type="TimeStamp">2025-01-05T08:00:00.000</value></field>
      </model>
    </modelType>
    <modelType type="Journey">
      <model type="Journey" id="j1">
        <field name="Name" type="String"><value type="String"><![CDATA[To the lake]]></value></field>
        <field name="StartTime" type="TimeStamp"><value type="TimeStamp">2025-01-05T09:00:00.000+00:00</value></field>
        <multiModelField name="WayPoints" type="Location">
          <model type="Location" id="wp1">
            <field name="Description" type="String"><value type="String"><![CDATA[Turn left]]></value></field>
            <modelField name="Position" type="Coordinate">{_coordinate("wc1", "46.2", "6.2")}</modelField>
          </model>
          <model type="Location" id="wp2">
            <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-05T09:30:00.000+00:00</value></field>
            <modelField name="Position" type="Coordinate">{_coordinate("wc2", "46.3", "6.3")}</modelField>
          </model>
        </multiModelField>
      </model>
    </modelType>
    <modelType type="SearchedItem">
      <model type="SearchedItem" id="s1">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-05T10:00:00.000+00:00</value></field>
        <field name="Value" type="String"><value type="String"><![CDATA[lake geneva]]></value></field>
      </model>
    </modelType>
    <modelType type="SocialMediaActivity">
      <model type="SocialMediaActivity" id="sm1">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-05T11:00:00.000+00:00</value></field>
        <modelField name="Position" type="Coordinate">{_coordinate("smc1", "46.4", "6.4")}</modelField>
      </model>
      <model type="SocialMediaActivity" id="sm2">
        <field name="TimeStamp" type="TimeStamp"><value type="TimeStamp">2025-01-05T12:00:00.000+00:00</value></field>
      </model>
    </modelType>
  </decodedData>
  <extraInfos>
    <extraInfo type="model" id="chat1"><sourceInfo><nodeInfos><nodeInfo name="msgstore.db" path="FFS.zip/msgstore.db" tableName="messages" offset="10" /></nodeInfos></sourceInfo></extraInfo>
  </extraInfos>
</project>
"""


def _header(path: Path, name: str) -> tuple[str, ...]:
    return next(load_workbook(path, read_only=True)[name].iter_rows(values_only=True))


def _bare(column: str) -> str:
    for prefix in ("[report] ", "[tool] "):
        if column.startswith(prefix):
            return column[len(prefix):]
    return column


def _sheet(path: Path, name: str) -> list[dict[str, str | None]]:
    """Rows keyed by BARE column names (origin prefixes are asserted by test_column_origins)."""
    workbook = load_workbook(path, read_only=True)
    rows = workbook[name].iter_rows(values_only=True)
    header = [_bare(column) for column in next(rows)]
    return [dict(zip(header, row)) for row in rows]


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "wb_Report.xml"
    path.write_text(REPORT, encoding="utf-8")
    return path


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ufdr_config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def workbook(report: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)  # no ./ufdr_config.toml here: the built-in default applies
    summary = run_dump(report, tmp_path / "out")
    assert [(b.name, b.status) for b in summary.bricks] == [("workbook", "ok")]
    path = tmp_path / "out" / "wb_Report_combined.xlsx"
    assert summary.bricks[0].files == [path]
    return path


def test_sheets_are_fixed_and_in_order(workbook: Path) -> None:
    assert load_workbook(workbook, read_only=True).sheetnames == [
        "About", "Documentation", "Source device", "Communications", "Identifiers", "Locations",
    ]


def test_chat_messages_with_to_basis(workbook: Path) -> None:
    rows = {r["Record uuid"]: r for r in _sheet(workbook, "Communications")}
    m1, m2, m3, im1 = rows["m1"], rows["m2"], rows["m3"], rows["im1"]
    # m1 states only an EMPTY To party (ignored), so To is inferred from the participants.
    assert m1["From"] == "Alice <+41790000001> [flagged as phone owner]"
    assert m1["To"] == "Bob <+41790000002>"
    assert m1["To basis"] == "inferred from chat participants"
    assert m1["Body"] == "Salut, ça va ?"
    assert m1["Chat"] == "Famille" and m1["Chat app"] == "WhatsApp"
    assert m2["To basis"] == "stated"
    # The sender is not a participant: "everyone else" cannot be told, so nothing is inferred.
    assert m3["To"] is None and m3["To basis"] == "not stated"
    assert im1["com_type"] == "Instant message" and im1["To basis"] == "not stated"
    # Nested rows inherit the chat's source; every row names the report.
    assert m1["source_path"] == "FFS.zip/msgstore.db"
    assert m1["report_file"].endswith("wb_Report.xml")


def test_values_stay_text(workbook: Path) -> None:
    cells = {c.value: c.data_type for row in load_workbook(workbook)["Communications"].iter_rows() for c in row}
    assert cells["=SUM(1,2)"] == "s"  # not a live formula
    rows = {r["Record uuid"]: r for r in _sheet(workbook, "Communications")}
    assert rows["m1"]["From identifiers"] == "+41790000001"  # leading + kept


def test_every_communication_type_on_one_sheet(workbook: Path) -> None:
    unified = _sheet(workbook, "Communications")
    assert [r["com_type"] for r in unified] == [
        "Chat message", "Chat message", "Chat message", "Instant message", "Call", "Email",
    ]
    # Every type's other fields follow the shared columns, verbatim; blank where absent.
    by_id = {r["Record uuid"]: r for r in unified}
    call, email = by_id["call1"], by_id["mail1"]
    assert (call["Direction"], call["Duration"], call["To"]) == ("Outgoing", "00:01:00", "Bob <+41790000002>")
    assert email["Subject"] == "Réunion"
    assert email["Other parties"] == "[Cc] c@x.ch"
    assert email["Attachments"] == "plan.pdf (files/plan.pdf)"
    assert email["TimeStamp.offset"] is None  # no offset determinable: blank, no default


def test_identifiers_sheet(workbook: Path) -> None:
    rows = _sheet(workbook, "Identifiers")
    assert set(rows[0]) == {"Identifier", "Kind", "Source file", "CSV file", "Occurrences"}
    by_key = {(r["Identifier"], r["Kind"], r["CSV file"]): r for r in rows}
    alice_chat = by_key[("+41790000001", "Party.Identifier", "wb_Report_SSMParty.csv")]
    assert alice_chat["Source file"] == "FFS.zip/msgstore.db" and alice_chat["Occurrences"] == "2"
    assert by_key[("+41790000002", "PhoneNumber.Value", "wb_Report_SMPhoneNumber.csv")]["Occurrences"] == "1"
    # Device information: Kind is the record's own label; only the configured names are kept.
    assert ("353872860051601", "IMEI", "wb_Report_DeviceInfoEntry.csv") in by_key
    assert rows == sorted(rows, key=lambda r: (r["Identifier"], r["Kind"], r["Source file"] or "", r["CSV file"]))


def test_locations_sheet(workbook: Path) -> None:
    location = next(r for r in _sheet(workbook, "Locations") if r["Record uuid"] == "loc1")
    assert (location["Latitude"], location["Longitude"], location["TimeStamp.offset"]) == ("46.5", "6.6", "+00:00")


def test_about_documents_every_sheet(workbook: Path) -> None:
    about = [row for row in load_workbook(workbook, read_only=True)["About"].iter_rows(values_only=True)]
    sheets = {row[0]: row[2] for row in about if row and row[0] in ("Communications", "Locations") and row[2] and row[2].isdigit()}
    assert sheets == {"Communications": "6", "Locations": "7"}
    documentation = list(load_workbook(workbook, read_only=True)["Documentation"].iter_rows(values_only=True))
    assert any(row[:3] == ("Communications", "[tool] To basis", "tool") for row in documentation)
    assert any(row[:3] == ("Locations", "[report] Latitude", "report") for row in documentation)


def test_parallel_parse_gives_same_workbook(report: Path, tmp_path: Path, workbook: Path) -> None:
    run_dump(report, tmp_path / "par", parse_workers=2)
    parallel = tmp_path / "par" / "wb_Report_combined.xlsx"
    for name in ("Source device", "Communications", "Identifiers", "Locations"):
        assert _sheet(parallel, name) == _sheet(workbook, name)


def test_no_bricks_writes_csvs_only(report: Path, tmp_path: Path) -> None:
    summary = run_dump(report, tmp_path / "out", bricks=set())
    assert summary.bricks == []
    assert not list((tmp_path / "out").glob("*.xlsx"))
    assert summary.files


def test_failing_brick_does_not_fail_dump(report: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        name = "broken"
        description = "always fails"

        def run(self, output):
            raise RuntimeError("boom")

    monkeypatch.setattr(bricks_base, "_REGISTRY", {"broken": Broken()})
    summary = run_dump(report, tmp_path / "out")
    assert summary.bricks[0].status == "failed" and "boom" in summary.bricks[0].error
    assert summary.files  # the CSVs stand


def test_excel_limits(report: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(xlsx, "EXCEL_MAX_ROWS", 3)  # header + 2 rows per sheet part
    monkeypatch.setattr(xlsx, "EXCEL_MAX_CELL_CHARS", 160)
    path = tmp_path / "limits.xml"
    path.write_text(REPORT.replace("SMS seul", "x" * 500), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    run_dump(path, tmp_path / "out")
    book = load_workbook(tmp_path / "out" / "limits_combined.xlsx", read_only=True)
    assert "Communications (2)" in book.sheetnames
    rows = _sheet(tmp_path / "out" / "limits_combined.xlsx", "Communications (2)")
    body = rows[-1]["Body"]
    assert len(body) == 160 and "(500 characters)" in body


@pytest.mark.parametrize(
    ("value", "shown"),
    [("a\x0bb", "a[U+000B]b"), ("a\ufffeb", "a[U+FFFE]b"), ("a\uffffb", "a[U+FFFF]b"), ("a\ud83db", "a[U+D83D]b")],
)
def test_characters_xml_cannot_carry_are_escaped_not_dropped(tmp_path: Path, value: str, shown: str) -> None:
    # XML 1.0 cannot carry them, so they cannot come through a report; CSV text can.
    stats = xlsx.WriteStats()
    book = xlsx.open_workbook()
    writer = xlsx.SheetWriter(book, "t", ["h"], stats)
    writer.append([value])
    writer.close()
    book.save(tmp_path / "t.xlsx")  # the XML writer raises on any it is given
    assert list(load_workbook(tmp_path / "t.xlsx")["t"].values)[1][0] == shown
    assert stats.escaped_characters == 1


@pytest.mark.parametrize(
    ("value", "cut"),
    [("\U0001F600" * 20_000, True), ("x" + "\U0001F600" * 20_000, True), ("\U0001F600" * 16_383, False), ("x" * 32_767, False)],
    ids=["emoji over the limit", "cut lands inside a pair", "emoji at the limit", "plain at the limit"],
)
def test_cell_limit_counts_what_excel_counts(value: str, cut: bool) -> None:
    stats = xlsx.WriteStats()
    text = xlsx.text_cell(xlsx.open_workbook().create_sheet("t"), value, stats).value
    assert stats.truncated_cells == int(cut)
    assert len(text.encode("utf-16-le")) // 2 <= xlsx.EXCEL_MAX_CELL_CHARS
    text.encode("utf-8")  # no half surrogate pair left behind
    assert (text == value) is not cut


def test_column_origins(workbook: Path) -> None:
    header = _header(workbook, "Communications")
    assert header[:3] == ("[tool] com_type", "[report] TimeStamp", "[tool] TimeStamp.offset")
    assert "[report] source_path" in header and "[tool] report_file" in header
    assert all(_bare(column) != "App" for column in header)


def test_source_device(workbook: Path) -> None:
    rows = _sheet(workbook, "Source device")
    by_name = {(r["Section"], r["Name"]): r for r in rows}
    assert by_name[("Case information", "Case name")]["Value"] == "Test case"
    start = by_name[("Metadata: Extraction Data", "DeviceInfoExtractionStartDateTime")]
    assert start["Value"] == "03/09/2025 17:35:59(UTC+2)" and start["Details"] == "sourceExtraction=0"
    assert by_name[("Image FileDump", "SHA256")]["Value"] == "ABCD"
    imei = by_name[("Device info: General", "IMEI")]
    assert imei["Value"] == "353872860051601" and imei["Record uuid"] == "dev1"
    assert [r["Section"] for r in rows][:1] == ["Report"]  # header first, decoded device info last


def test_locations_all_position_types(workbook: Path) -> None:
    rows = _sheet(workbook, "Locations")
    header = _header(workbook, "Locations")
    for removed in ("Time", "Time field", "Label", "Address", "App"):
        assert all(_bare(column) != removed for column in header)
    # Report order, type by type, no sorting.
    assert [r["Record uuid"] for r in rows] == ["loc1", "cell1", "wifi1", "wp1", "wp2", "s1", "sm1"]
    by_id = {r["Record uuid"]: r for r in rows}
    cell = by_id["cell1"]
    assert (cell["Record type"], cell["Latitude"], cell["Latitude field"]) == ("CellTower", "46.1", "SMCoordinate.Latitude")
    assert (cell["MCC"], cell["CID"]) == ("228", "42")  # own fields verbatim, not merged into a label
    wifi = by_id["wifi1"]
    assert (wifi["Latitude"], wifi["Latitude field"], wifi["LastConnection.offset"]) == (None, None, None)
    wp1 = by_id["wp1"]
    assert wp1["Record type"] == "Journey point" and wp1["Description"] == "Turn left"
    assert (wp1["Latitude"], wp1["Longitude field"]) == ("46.2", "SSMCoordinate.Longitude")
    assert (wp1["Journey.Name"], wp1["Journey.StartTime"]) == ("To the lake", "2025-01-05T09:00:00.000+00:00")
    assert wp1["TimeStamp"] is None  # never filled from the journey
    assert by_id["s1"]["Value"] == "lake geneva" and by_id["s1"]["Latitude"] is None
    # An unconfigured type is listed only for its records that carry a coordinate.
    assert "sm2" not in by_id


def test_group_message_is_one_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A chat message with several recipients is ONE row, its members together in To."""
    members = "".join(_party(f"gp{i}", f"+4179000010{i}", "General") for i in range(4))
    group = f"""<?xml version="1.0" encoding="utf-8"?>
<project xmlns="{NS}"><decodedData><modelType type="Chat">
  <model type="Chat" id="g1">
    <field name="Name" type="String"><value type="String"><![CDATA[Team]]></value></field>
    <multiModelField name="Participants" type="Party">{members}</multiModelField>
    <multiModelField name="Messages" type="InstantMessage">
      {_message("gm1", "2025-02-01T10:00:00.000+00:00", "inferred", _party("gs1", "+41790000100", "From"))}
    </multiModelField>
  </model>
</modelType></decodedData><extraInfos /></project>
"""
    path = tmp_path / "group.xml"
    path.write_text(group, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    run_dump(path, tmp_path / "out")
    rows = _sheet(tmp_path / "out" / "group_combined.xlsx", "Communications")
    assert [r["Record uuid"] for r in rows] == ["gm1"]
    assert rows[0]["To"].count("+4179000010") == 3
