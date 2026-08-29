#!/usr/bin/env python3
"""Assert the rendered invoice's text columns using Poppler PDF coordinates."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ElementTree


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "renderer_fixture.json"
XHTML_NAMESPACE = {"x": "http://www.w3.org/1999/xhtml"}
TOLERANCE_POINTS = 0.35
ROW_TOLERANCE_POINTS = 1.25


def fail(message):
    raise AssertionError(message)


def close(values, label):
    if max(values) - min(values) > TOLERANCE_POINTS:
        fail("%s are not aligned: %r" % (label, values))


def render(locale, directory, name=None, mutate=None):
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot["locale"] = locale
    if mutate is not None:
        mutate(snapshot)
    suffix = name if name is not None else locale
    output = directory / ("PPFlight-layout-%s.pdf" % suffix)
    env = os.environ.copy()
    local_autoload = ROOT / "renderer" / "vendor" / "autoload.php"
    fallback_autoload = Path("/www/wwwroot/www.ppflight.com/laravel/vendor/autoload.php")
    if not local_autoload.is_file() and fallback_autoload.is_file():
        env["PPFLIGHT_DOMPDF_TEST_AUTOLOAD"] = str(fallback_autoload)
    result = subprocess.run(
        [
            "php",
            str(ROOT / "renderer" / "bin" / "render.php"),
            "--output",
            str(output),
            "--cache-dir",
            str(directory),
        ],
        input=json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        fail("renderer failed for %s: %s" % (locale, result.stderr.decode("utf-8", "replace")))
    return output


def pdf_rows(pdf, directory):
    pdftotext = shutil.which("pdftotext")
    if pdftotext is None:
        fail("pdftotext is required for the PDF coordinate regression test")
    bbox = directory / (pdf.stem + ".html")
    result = subprocess.run(
        [pdftotext, "-bbox-layout", str(pdf), str(bbox)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        fail("pdftotext failed: %s" % result.stderr.decode("utf-8", "replace"))
    root = ElementTree.parse(str(bbox)).getroot()
    words = []
    for word in root.findall(".//x:word", XHTML_NAMESPACE):
        words.append(
            {
                "text": word.text or "",
                "x_min": float(word.attrib["xMin"]),
                "x_max": float(word.attrib["xMax"]),
                "y_min": float(word.attrib["yMin"]),
                "y_max": float(word.attrib["yMax"]),
            }
        )
    rows = []
    for word in sorted(words, key=lambda item: (item["y_min"], item["x_min"])):
        if not rows or abs(word["y_min"] - rows[-1]["anchor_y"]) > ROW_TOLERANCE_POINTS:
            rows.append({"anchor_y": word["y_min"], "words": [word]})
        else:
            rows[-1]["words"].append(word)
            rows[-1]["anchor_y"] = sum(item["y_min"] for item in rows[-1]["words"]) / len(rows[-1]["words"])
    for row in rows:
        row["words"].sort(key=lambda item: item["x_min"])
        row["text"] = " ".join(item["text"] for item in row["words"])
        row["y_min"] = min(item["y_min"] for item in row["words"])
    return rows, words


def phrase_spans(row, phrase):
    tokens = phrase.split(" ")
    matches = []
    for index in range(0, len(row["words"]) - len(tokens) + 1):
        candidate = row["words"][index : index + len(tokens)]
        if [word["text"] for word in candidate] == tokens:
            matches.append(
                {
                    "x_min": candidate[0]["x_min"],
                    "x_max": candidate[-1]["x_max"],
                    "words": candidate,
                }
            )
    return matches


def row_with_phrases(rows, *phrases):
    matches = [row for row in rows if all(phrase_spans(row, phrase) for phrase in phrases)]
    if len(matches) != 1:
        fail("expected one coordinate row containing %r, found %d" % (phrases, len(matches)))
    return matches[0]


def one_span(row, phrase, after=None):
    spans = phrase_spans(row, phrase)
    if after is not None:
        spans = [span for span in spans if span["x_min"] > after]
    if len(spans) != 1:
        fail("expected one %r span in %r, found %d" % (phrase, row["text"], len(spans)))
    return spans[0]


def assert_summary(rows, pairs):
    key_right_edges = []
    value_left_edges = []
    gaps = []
    for key, value in pairs:
        row = row_with_phrases(rows, key, value)
        key_span = one_span(row, key)
        value_span = one_span(row, value, after=key_span["x_max"])
        key_right_edges.append(key_span["x_max"])
        value_left_edges.append(value_span["x_min"])
        gaps.append(value_span["x_min"] - key_span["x_max"])
    close(key_right_edges, "summary label right edges")
    close(value_left_edges, "summary value left edges")
    close(gaps, "summary key/value gaps")
    if not all(8.5 <= gap <= 9.5 for gap in gaps):
        fail("summary key/value gap is not the fixed 12px CSS gap: %r" % gaps)


def assert_table_grid(rows):
    header = row_with_phrases(rows, "项目说明", "数量", "单价", "小计")
    item = row_with_phrases(rows, "KVM instance · LAX", "1", "USD 12.50")
    quantity_header = one_span(header, "数量")
    unit_header = one_span(header, "单价")
    total_header = one_span(header, "小计")
    quantity = one_span(item, "1")
    item_amounts = sorted(phrase_spans(item, "USD 12.50"), key=lambda span: span["x_min"])
    if len(item_amounts) != 2:
        fail("expected distinct unit-cost and line-total amounts on the item row")
    close([quantity_header["x_max"], quantity["x_max"]], "quantity column right edge")
    close([unit_header["x_max"], item_amounts[0]["x_max"]], "unit-cost column right edge")
    close([total_header["x_max"], item_amounts[1]["x_max"]], "line-total column right edge")
    if item_amounts[1]["x_max"] - item_amounts[0]["x_max"] < 100:
        fail("unit-cost and line-total values did not render in separate columns")

    totals = [
        ("税前金额", "USD 12.50"),
        ("优惠", "USD 0.00"),
        ("税费", "USD 0.00"),
        ("账单总额", "USD 12.50"),
        ("已支付", "USD 12.50"),
        ("剩余应付", "USD 0.00"),
    ]
    totals_right_edges = []
    for label, value in totals:
        row = row_with_phrases(rows, label, value)
        label_span = one_span(row, label)
        totals_right_edges.append(one_span(row, value, after=label_span["x_max"])["x_max"])
    close([item_amounts[1]["x_max"]] + totals_right_edges, "line-total and totals amount right edges")


def assert_long_summary_is_contained(rows, words, display_number, status):
    invoice_words = [
        word
        for word in words
        if 100 <= word["y_min"] <= 240
        and (word["text"].startswith("INV-") or (word["text"] and set(word["text"]) == {"A"}))
    ]
    status_words = [
        word
        for word in words
        if 100 <= word["y_min"] <= 240 and word["text"] and set(word["text"]) == {"s"}
    ]
    if "".join(word["text"] for word in sorted(invoice_words, key=lambda item: (item["y_min"], item["x_min"]))) != display_number:
        fail("long display number was lost or reordered")
    if "".join(word["text"] for word in sorted(status_words, key=lambda item: (item["y_min"], item["x_min"]))) != status:
        fail("long status was lost or reordered")
    if max(word["x_max"] for word in invoice_words) > 297.75:
        fail("long display number crossed the left summary half")
    if max(word["x_max"] for word in status_words) > 552.80:
        fail("long status crossed the page content edge")
    party_row = row_with_phrases(rows, "开票方", "收票方")
    summary_bottom = max(word["y_max"] for word in invoice_words + status_words)
    if party_row["y_min"] - summary_bottom < 15:
        fail("wrapped summary values overlap the party section")


def main():
    with tempfile.TemporaryDirectory(prefix="ppflight-renderer-layout-") as temp:
        directory = Path(temp)
        zh_rows, _ = pdf_rows(render("zh_CN", directory), directory)
        assert_summary(
            zh_rows,
            [
                ("账单编号", "INV-20260826-1042"),
                ("开具日期", "2026-08-26"),
                ("付款截止", "2026-09-02"),
            ],
        )
        assert_summary(
            zh_rows,
            [("状态", "paid"), ("币种", "USD"), ("付款时间", "2026-08-26")],
        )
        assert_table_grid(zh_rows)

        en_rows, _ = pdf_rows(render("en_US", directory), directory)
        assert_summary(
            en_rows,
            [
                ("Reference", "INV-20260826-1042"),
                ("Issued", "2026-08-26"),
                ("Payment due", "2026-09-02"),
            ],
        )
        assert_summary(
            en_rows,
            [("Status", "paid"), ("Currency", "USD"), ("Paid at", "2026-08-26")],
        )

        long_display_number = "INV-" + ("A" * 60)
        long_status = "s" * 32

        def use_long_summary(snapshot):
            snapshot["invoice"]["display_number"] = long_display_number
            snapshot["invoice"]["status"] = long_status

        long_rows, long_words = pdf_rows(
            render("zh_CN", directory, name="long-summary", mutate=use_long_summary),
            directory,
        )
        assert_long_summary_is_contained(long_rows, long_words, long_display_number, long_status)

    print("renderer layout tests passed")


if __name__ == "__main__":
    main()
