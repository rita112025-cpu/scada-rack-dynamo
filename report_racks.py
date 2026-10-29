# Equipment register (Excel) and dashboard (HTML) for SCADA_Racks_SoW_V6.csv, runnable without Revit.
#
# Usage: python report_racks.py [csv path] [--out 資料夾] [--force]
#   Writes 報表\SCADA_設備清冊.xlsx and 報表\SCADA_儀表板.html (next to this script unless --out).
#   Runs validate_racks first: any [錯誤] stops before anything is written, unless --force,
#   which writes both files marked「資料有錯誤」.
#   Each file is written to .<name>.tmp in the same folder and then swapped in; when that fails
#   (e.g. the workbook is open in Excel) the old file is kept.
#   Exit code 0 only when the data has no errors and both files were updated.
#   The Excel file needs openpyxl (not installed automatically); the HTML file needs only the stdlib.
#   No network access; writes only inside the output folder.

import argparse
import csv
import datetime
import html
import os
import sys

import validate_racks as v

OUTPUT_DIR = "報表"
XLSX_NAME = "SCADA_設備清冊.xlsx"
HTML_NAME = "SCADA_儀表板.html"
NUMBER_COLUMNS = v.SIZE_COLUMNS + v.OPTIONAL_NUMBER_COLUMNS + ("附錄D項次", "附錄F項次")
EXPECTED_ROOM_COLUMN = "預計房間（預估）"
TYPE_COLUMN = "機櫃類型"
ERROR_BANNER = "資料有錯誤（以 --force 輸出），請先修正 CSV"


# ---------- data ----------

def header(rows):
    return [c for c in rows[0].keys() if c is not None]


def text(row, column):
    return row.get(column) or ""


def cell_value(column, value):
    """Numbers for numeric columns when they parse; the original text otherwise."""
    if column in NUMBER_COLUMNS:
        n = v._finite(value.strip())
        if n is not None:
            return int(n) if n.is_integer() else n
    return value


def expected_room(row, base_dir, room_cache):
    model = text(row, v.MODEL_COLUMN).strip()
    if not model:
        return "原點（無建築模型）"
    rooms = room_cache.get(os.path.normcase(os.path.normpath(os.path.join(base_dir, model))))
    if rooms is None:
        return "未知（房間快取沒有此模型）"
    room = v.match_room(rooms, text(row, v.ROOM_COLUMN))
    if room is None:
        return "原點（房間關鍵字未命中）"
    return (room["number"] + " " + room["name"]).strip()


def station_counts(rows):
    """Return (sorted groups, sorted rack types, {(group, type): count})."""
    counts = {}
    for r in rows:
        key = (text(r, v.GROUP_COLUMN), text(r, TYPE_COLUMN))
        counts[key] = counts.get(key, 0) + 1
    groups = sorted({g for g, _ in counts})
    types = sorted({t for _, t in counts})
    return groups, types, counts


class Report:
    def __init__(self, rows, issues, base_dir, room_cache, generated):
        self.rows = rows
        self.columns = header(rows)
        self.issues = issues
        self.has_errors = any(i.level == v.ERROR for i in issues)
        self.pending = sum(1 for r in rows if v.PENDING_MARK in text(r, v.NOTE_COLUMN))
        self.rooms = [expected_room(r, base_dir, room_cache) for r in rows]
        self.groups, self.types, self.counts = station_counts(rows)
        self.generated = generated


# ---------- output files ----------

def replace_atomically(path, write):
    """write(tmp) next to path, then swap it in; on failure the old file stays and tmp is removed."""
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _display_width(value):
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(value))


def write_xlsx(path, report):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    bold = Font(bold=True)
    red = PatternFill("solid", fgColor="F4CCCC")

    def put(ws, r, c, value):
        if isinstance(value, str):  # only reachable with --force; validate_racks rejects these
            value = v.CONTROL_CHARS.sub("�", value)
        cell = ws.cell(r, c, value)
        if cell.data_type == "f":  # CSV text starting with "=" stays text, never a formula
            cell.data_type = "s"
        return cell

    def put_row(ws, r, values, font=None):
        for c, value in enumerate(values, start=1):
            cell = put(ws, r, c, value)
            if font:
                cell.font = font

    def fit(ws):
        for c, col in enumerate(ws.iter_cols(values_only=True), start=1):
            width = max((_display_width(x) for x in col if x is not None), default=4)
            ws.column_dimensions[get_column_letter(c)].width = min(width + 2, 60)

    wb = Workbook()
    ws = wb.active
    ws.title = "設備清冊"
    put_row(ws, 1, report.columns + [EXPECTED_ROOM_COLUMN], bold)
    for r, (row, room) in enumerate(zip(report.rows, report.rooms), start=2):
        put_row(ws, r, [cell_value(c, text(row, c)) for c in report.columns] + [room])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    fit(ws)

    ws = wb.create_sheet("各站統計")
    put_row(ws, 1, [v.GROUP_COLUMN] + report.types + ["合計"], bold)
    for r, g in enumerate(report.groups, start=2):
        values = [report.counts.get((g, t), 0) for t in report.types]
        put_row(ws, r, [g] + values + [sum(values)])
    totals = [sum(report.counts.get((g, t), 0) for g in report.groups) for t in report.types]
    put_row(ws, len(report.groups) + 2, ["合計"] + totals + [sum(totals)], bold)
    ws.freeze_panes = "B2"
    fit(ws)

    ws = wb.create_sheet("檢查結果")
    put_row(ws, 1, ["產生時間", report.generated])
    if report.has_errors:
        put(ws, 2, 1, ERROR_BANNER).fill = red
    put_row(ws, 3, ["等級", "編號／群組", "欄位", "說明"], bold)
    for r, i in enumerate(report.issues, start=4):
        put_row(ws, r, list(i))
        if i.level == v.ERROR:
            for c in range(1, 5):
                ws.cell(r, c).fill = red
    fit(ws)
    wb.save(path)


HTML_STYLE = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e2e2dc;--accent:#2f5d8a;
--warn-bg:#fff4d6;--warn-fg:#7a5600;--err-bg:#fde2e1;--err-fg:#9b1c1c}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161615;--fg:#ececea;--muted:#a3a39d;
--card:#21211f;--line:#34342f;--accent:#8db7e0;--warn-bg:#3a3014;--warn-fg:#f2d48a;--err-bg:#3d1b1b;--err-fg:#f5b4b4}}
:root[data-theme="dark"]{--bg:#161615;--fg:#ececea;--muted:#a3a39d;--card:#21211f;--line:#34342f;--accent:#8db7e0;
--warn-bg:#3a3014;--warn-fg:#f2d48a;--err-bg:#3d1b1b;--err-fg:#f5b4b4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 "Microsoft JhengHei","Noto Sans TC",system-ui,sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:32px 0 10px}
.meta{color:var(--muted);margin:0 0 20px}
.banner{background:var(--err-bg);color:var(--err-fg);padding:12px 16px;border-radius:8px;font-weight:700;margin-bottom:20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.tile b{display:block;font-size:28px;font-variant-numeric:tabular-nums;color:var(--accent)}
.tile span{color:var(--muted);font-size:13px}
.scroll{overflow:auto;max-height:70vh;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);font-weight:600}
td.n{text-align:right;font-variant-numeric:tabular-nums}
tr.total td{font-weight:700}
.lv-錯誤{background:var(--err-bg);color:var(--err-fg)}.lv-警告{background:var(--warn-bg);color:var(--warn-fg)}
"""


def _e(value):
    return html.escape(str(value), quote=True)


def _table(head, body_rows, numeric=(), row_class=None):
    out = ["<div class=\"scroll\"><table><thead><tr>"]
    out += ["<th>%s</th>" % _e(h) for h in head]
    out.append("</tr></thead><tbody>")
    for k, values in enumerate(body_rows):
        cls = row_class(k) if row_class else ""
        out.append("<tr%s>" % (" class=\"%s\"" % _e(cls) if cls else ""))
        out += ["<td%s>%s</td>" % (" class=\"n\"" if c in numeric else "", _e(x)) for c, x in enumerate(values)]
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def render_html(report):
    errors = sum(1 for i in report.issues if i.level == v.ERROR)
    tiles = [(len(report.rows), "機櫃總台數"), (len(report.groups), "檔案群組（站）"),
             (report.pending, "備註待確認"), (errors, "錯誤"), (len(report.issues) - errors, "警告")]
    station_rows = []
    for g in report.groups:
        values = [report.counts.get((g, t), 0) for t in report.types]
        station_rows.append([g] + values + [sum(values)])
    totals = [sum(report.counts.get((g, t), 0) for g in report.groups) for t in report.types]
    station_rows.append(["合計"] + totals + [sum(totals)])
    numeric = range(1, len(report.types) + 2)
    rack_columns = [v.CODE_COLUMN, v.GROUP_COLUMN, "站點", "房間", TYPE_COLUMN, "尺寸", v.NOTE_COLUMN]
    rack_rows = [[text(r, c) for c in rack_columns] + [room] for r, room in zip(report.rows, report.rooms)]

    parts = [
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<title>SCADA 機櫃儀表板</title><style>%s</style></head><body><main>" % HTML_STYLE,
        "<h1>SCADA 機櫃儀表板</h1>",
        "<p class=\"meta\">SoW V6 附錄 D · 產生時間 %s · 「預計房間」為依房間快取推算的預估值，"
        "實際結果以 Revit 執行後的 SCADA_定位來源 為準</p>" % _e(report.generated),
    ]
    if report.has_errors:
        parts.append("<div class=\"banner\">%s</div>" % _e(ERROR_BANNER))
    parts.append("<div class=\"tiles\">")
    parts += ["<div class=\"tile\"><b>%s</b><span>%s</span></div>" % (_e(n), _e(label)) for n, label in tiles]
    parts.append("</div><h2>各站台數</h2>")
    parts.append(_table([v.GROUP_COLUMN] + report.types + ["合計"], station_rows, numeric,
                        lambda k: "total" if k == len(station_rows) - 1 else ""))
    parts.append("<h2>檢查結果</h2>")
    if report.issues:
        parts.append(_table(["等級", "編號／群組", "欄位", "說明"], [list(i) for i in report.issues],
                            row_class=lambda k: "lv-" + report.issues[k].level))
    else:
        parts.append("<p>沒有問題。</p>")
    parts.append("<h2>機櫃清單</h2>")
    parts.append(_table(rack_columns + [EXPECTED_ROOM_COLUMN], rack_rows))
    parts.append("</main></body></html>\n")
    return "".join(parts)


def write_html(path, report):
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html(report))


# ---------- command line ----------

def parse_args(argv):
    tool_dir = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(prog="report_racks.py", description="產生 SCADA 機櫃 Excel 設備清冊與 HTML 儀表板")
    p.add_argument("csv", nargs="?", default=os.path.join(tool_dir, v.DEFAULT_CSV), help="機櫃 CSV（預設為工具資料夾內的 CSV）")
    p.add_argument("--out", default=os.path.join(tool_dir, OUTPUT_DIR), help="輸出資料夾（預設為工具資料夾\\報表）")
    p.add_argument("--force", action="store_true", help="CSV 有錯誤仍輸出，報表會標示「資料有錯誤」")
    return p.parse_args(argv)


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    args = parse_args(argv)
    try:
        rows = v.read_rows(args.csv)
    except (OSError, UnicodeDecodeError, csv.Error) as ex:
        print("[錯誤] 無法讀取 %s: %s" % (args.csv, ex))
        return 1
    base_dir = os.path.dirname(os.path.abspath(args.csv))
    issues = v.validate(rows, base_dir)
    for i in issues:
        print("[%s] %s %s: %s" % i)
    has_errors = any(i.level == v.ERROR for i in issues)
    if has_errors and not args.force:
        print("CSV 有錯誤，不輸出報表（修正後重跑，或加 --force 強制輸出）")
        return 1
    if not rows:
        return 1

    report = Report(rows, issues, base_dir, v.load_room_cache(base_dir),
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    try:
        os.makedirs(args.out, exist_ok=True)
    except OSError as ex:
        print("[錯誤] 無法建立輸出資料夾 %s: %s" % (args.out, ex))
        return 1
    updated = failed = 0
    for name, writer in ((XLSX_NAME, write_xlsx), (HTML_NAME, write_html)):
        path = os.path.join(args.out, name)
        try:
            replace_atomically(path, lambda tmp: writer(tmp, report))
            updated += 1
            print("%s: 已更新 %s" % (name, path))
        except ImportError:
            failed += 1
            print("%s: 保留舊檔：未安裝 openpyxl（不會自動安裝）" % name)
        except Exception as ex:  # one file failing must not stop the other
            failed += 1
            print("%s: 保留舊檔：%s: %s" % (name, type(ex).__name__, ex))
    errors = sum(1 for i in issues if i.level == v.ERROR)
    print("結果：資料錯誤 %d、警告 %d；報表 已更新 %d／失敗 %d" % (errors, len(issues) - errors, updated, failed))
    return 0 if not failed and not has_errors else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
