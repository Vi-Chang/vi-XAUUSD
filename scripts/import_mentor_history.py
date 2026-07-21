"""一次性匯入:老師帶單歷史紀錄(IMPORT-MENTOR-HISTORY.md)。

用法:
  python scripts/import_mentor_history.py [路徑,預設 mentor_trades_XAUUSD.json]
  (連線目標由 DATABASE_URL 決定;要匯入雲端 Postgres 就覆寫該環境變數)

規則:
- 對帳先行:任何一項與預期不符 → 直接中止,一筆都不寫。
- 冪等:以 (account_no, close_time, entry, exit) 判重 + DB 唯一索引雙保險,重跑不重複。
- 缺停損/停利就是 null,絕不回填(避免產生假的 R-multiple)。
- status='CLOSED'、is_active=False:與進行中訊號完全分離,不進任何比對/決策。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMPORT_BATCH = "MENTOR_HISTORY_2026Q2"
SOURCE_TZ = ZoneInfo("Asia/Taipei")   # 資料檔時間為 UTC+8

# ── 對帳基準(IMPORT-MENTOR-HISTORY.md Step 4;不吻合即中止)──
EXPECTED = {
    "count": 32, "wins": 21, "losses": 11,
    "net_pl": 1436.30, "net_after_fees": 1426.97,
    "gross_profit": 5751.22, "gross_loss": -4314.92,
    "profit_factor": 1.333,
}
POINTS_TOLERANCE = 0.05   # points × lots × 100 ≈ pl_usd


def fail(msg: str) -> None:
    print(f"❌ 對帳失敗,中止匯入(未寫入任何資料):{msg}")
    sys.exit(1)


def validate(trades: list[dict]) -> dict:
    n = len(trades)
    pls = [t["pl_usd"] for t in trades]
    wins = [x for x in pls if x > 0]
    losses = [x for x in pls if x < 0]
    stats = {
        "count": n, "wins": len(wins), "losses": len(losses),
        "net_pl": round(sum(pls), 2),
        "net_after_fees": round(sum(t["net_usd"] for t in trades), 2),
        "gross_profit": round(sum(wins), 2),
        "gross_loss": round(sum(losses), 2),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
    }
    for key, expected in EXPECTED.items():
        got = stats[key]
        if isinstance(expected, float):
            if abs(got - expected) > 0.005 and key != "profit_factor":
                fail(f"{key}: 得 {got},預期 {expected}")
            if key == "profit_factor" and abs(got - expected) > 0.001:
                fail(f"{key}: 得 {got},預期 {expected}")
        elif got != expected:
            fail(f"{key}: 得 {got},預期 {expected}")
    # 每筆驗算:points × lots × 100 = pl_usd(誤差 < 0.05)
    for t in trades:
        calc = t["points"] * t["lots"] * 100
        if abs(calc - t["pl_usd"]) >= POINTS_TOLERANCE:
            fail(f"{t['id']}: points×lots×100={calc:.2f} 與 pl_usd={t['pl_usd']} 差距過大")
        expected_pts = (t["exit"] - t["entry"]) if t["side"] == "BUY" else (t["entry"] - t["exit"])
        if abs(expected_pts - t["points"]) >= POINTS_TOLERANCE:
            fail(f"{t['id']}: points 欄位 {t['points']} 與價差算得 {expected_pts:.2f} 不符")
    return stats


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "mentor_trades_XAUUSD.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    trades = data["trades"]
    account_no = str(data.get("account", ""))

    stats = validate(trades)
    print(f"✅ 對帳通過:{stats['count']} 筆,勝/負 {stats['wins']}/{stats['losses']},"
          f"淨損益 {stats['net_pl']:+.2f}(扣費後 {stats['net_after_fees']:+.2f}),"
          f"毛利/毛損 {stats['gross_profit']:+.2f}/{stats['gross_loss']:+.2f},"
          f"獲利因子 {stats['profit_factor']}")

    from app.db.models import MentorSignal
    from app.db.session import db_session, init_db
    init_db()

    inserted = skipped = 0
    now = datetime.now(timezone.utc)
    with db_session() as db:
        for t in trades:
            close_utc = (datetime.strptime(t["close_time"], "%Y-%m-%d %H:%M:%S")
                         .replace(tzinfo=SOURCE_TZ).astimezone(timezone.utc))
            exists = db.query(MentorSignal).filter(
                MentorSignal.account_no == account_no,
                MentorSignal.close_time == close_utc,
                MentorSignal.entry_price == t["entry"],
                MentorSignal.close_price == t["exit"],
            ).first()
            if exists:
                skipped += 1
                continue
            db.add(MentorSignal(
                direction="LONG" if t["side"] == "BUY" else "SHORT",
                entry_price=t["entry"], stop_loss=None, targets=[],
                note=f"歷史匯入 {t['id']}(TMGM App 截圖;無停損/停利/開倉時間資料)",
                signal_time=close_utc,   # 無開倉時間,以平倉時間代表(僅排序用)
                is_active=False, created_at=now,
                status="CLOSED", open_time=None, close_time=close_utc,
                close_price=t["exit"], lots=t["lots"],
                pl_usd=t["pl_usd"], swap_usd=t["swap_usd"], net_usd=t["net_usd"],
                points=t["points"], r_multiple=None, r_source="UNKNOWN",
                import_batch=IMPORT_BATCH, account_no=account_no,
            ))
            inserted += 1

    print(f"匯入完成:新增 {inserted} 筆、略過(已存在){skipped} 筆")
    for gap in data.get("known_gaps", []):
        print(f"⚠ 已知資料缺口:{gap}(非該期間空手,僅無紀錄)")

    # 匯入後以 DB 重新統計覆核
    from app.services.mentor_service import history_block
    s = history_block()["summary"]
    print(f"DB 覆核:{s['count']} 筆,勝/負 {s['wins']}/{s['losses']},"
          f"淨損益 {s['net_pl_usd']:+.2f}(扣費後 {s['net_after_fees_usd']:+.2f}),"
          f"獲利因子 {s['profit_factor']}")


if __name__ == "__main__":
    main()
