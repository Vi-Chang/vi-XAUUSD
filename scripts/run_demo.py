"""模擬資料一鍵展示(spec 二十七之15):無任何 API Key 執行完整分析一輪。

用法:python scripts/run_demo.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MOCK_DATA_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///./xauusd_demo.db")
os.environ.setdefault("DISABLE_SCHEDULER", "true")


async def main() -> None:
    from app.config import get_settings
    get_settings.cache_clear()
    from app.db.session import init_db
    from app.logging_config import setup_logging
    from app.providers.mock import MockProvider
    from app.services.analysis_service import run_analysis

    setup_logging()
    init_db()
    result = await run_analysis(MockProvider(), trigger="demo")
    print("\n" + "=" * 60)
    print("固定輸出 JSON(spec 二十二)")
    print("=" * 60)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2, default=str))
    print("\n決策:", result.decision.action, "| 信心:", result.decision.confidence_grade,
          "| 證據分數:", result.decision.evidence_score)
    print("市場狀態:", result.market_state)
    print("現在最容易犯的錯:", result.most_likely_user_mistake_now)


if __name__ == "__main__":
    asyncio.run(main())
