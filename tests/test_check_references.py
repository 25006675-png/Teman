import csv
import check_references as cr

HEADER = ["clip_id", "file", "speaker_id", "reference_text",
          "expected_amount", "expected_payee"]


def write_csv(path, rows, header=HEADER, encoding="utf-8"):
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def make_audio(folder, *names):
    for name in names:
        (folder / name).write_bytes(b"x")


def test_good_rows(tmp_path):
    make_audio(tmp_path, "a1.m4a", "a2.m4a")
    write_csv(tmp_path / "r.csv", [
        ["c1", "a1.m4a", "s1", "text one", "", ""],
        ["c2", "a2.m4a", "s2", "text two", "42.50", "TNB"],
    ])
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert (r["rows"], r["ok_rows"], r["problems"]) == (2, 2, [])


def test_missing_audio_file(tmp_path):
    write_csv(tmp_path / "r.csv", [["c1", "nope.m4a", "s1", "text", "", ""]])
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert r["ok_rows"] == 0
    assert any("audio file not found" in p for p in r["problems"])


def test_empty_reference(tmp_path):
    make_audio(tmp_path, "a1.m4a")
    write_csv(tmp_path / "r.csv", [["c1", "a1.m4a", "s1", "", "", ""]])
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert any("reference_text is empty" in p for p in r["problems"])


def test_duplicate_clip_id(tmp_path):
    make_audio(tmp_path, "a1.m4a", "a2.m4a")
    write_csv(tmp_path / "r.csv", [
        ["c1", "a1.m4a", "s1", "text", "", ""],
        ["c1", "a2.m4a", "s1", "text", "", ""],
    ])
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert any("duplicate clip_id" in p for p in r["problems"])


def test_duplicate_file(tmp_path):
    make_audio(tmp_path, "a1.m4a")
    write_csv(tmp_path / "r.csv", [
        ["c1", "a1.m4a", "s1", "text", "", ""],
        ["c2", "a1.m4a", "s1", "text", "", ""],
    ])
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert any("duplicate file" in p for p in r["problems"])


def test_missing_column(tmp_path):
    write_csv(tmp_path / "r.csv", [["c1", "a1.m4a"]], header=["clip_id", "file"])
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert any("missing columns" in p for p in r["problems"])


def test_csv_not_found(tmp_path):
    r = cr.check_references(str(tmp_path / "nope.csv"), str(tmp_path))
    assert any("not found" in p for p in r["problems"])


def test_excel_bom_is_accepted(tmp_path):
    make_audio(tmp_path, "a1.m4a")
    write_csv(tmp_path / "r.csv", [["c1", "a1.m4a", "s1", "text", "", ""]],
              encoding="utf-8-sig")
    r = cr.check_references(str(tmp_path / "r.csv"), str(tmp_path))
    assert r["ok_rows"] == 1