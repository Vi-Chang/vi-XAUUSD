"""Provider Adapter Pattern(spec 二):任何資料來源可替換,不綁死單一平台。"""
from app.providers.base import Candle, MarketDataProvider, PriceTick

__all__ = ["Candle", "MarketDataProvider", "PriceTick", "get_primary_provider"]


def get_primary_provider() -> MarketDataProvider:
    """依設定回傳主要行情 Provider。

    PRIMARY_PROVIDER=auto:mock 模式 → mock;否則優先 MT5(TMGM),
    無 MT5 可用時退回 OANDA;兩者皆無 → mock(並記 log)。
    """
    import logging

    from app.config import get_settings
    s = get_settings()
    choice = s.primary_provider.lower()

    # mock 模式凌駕一切選擇(安全:沒 Key 也能展示,且測試絕不打真實 API)
    if choice == "mock" or s.mock_data_mode:
        from app.providers.mock import MockProvider
        return MockProvider()
    if choice == "twelve_data":
        from app.providers.twelve_data import TwelveDataProvider
        return TwelveDataProvider()
    if choice == "oanda":
        from app.providers.oanda import OandaProvider
        return OandaProvider()
    if choice == "capital":
        from app.providers.capital_com import CapitalComProvider
        return CapitalComProvider()
    if choice == "mt5":
        from app.providers.mt5_tmgm import Mt5Provider
        return Mt5Provider()

    # auto(非 mock):MT5 → Capital.com → OANDA → mock
    try:
        from app.providers.mt5_tmgm import Mt5Provider
        return Mt5Provider()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("MT5 不可用(%s),嘗試 Capital.com", exc)
    if s.capital_api_key:
        try:
            from app.providers.capital_com import CapitalComProvider
            return CapitalComProvider()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("Capital.com 不可用(%s),嘗試 OANDA", exc)
    if s.oanda_api_token:
        from app.providers.oanda import OandaProvider
        return OandaProvider()
    if s.twelve_data_api_key:
        from app.providers.twelve_data import TwelveDataProvider
        return TwelveDataProvider()
    logging.getLogger(__name__).warning("無可用實盤 Provider,退回 mock 模式")
    from app.providers.mock import MockProvider
    return MockProvider()


def get_fast_quote_provider() -> MarketDataProvider | None:
    """第 1 層報價層的快速報價源(1 分鐘級輪詢)。

    優先序:Capital.com(demo REST,免費無嚴格限流)→ OANDA。
    皆未設定時回傳 None → 第 1 層自動降級為主力 provider 的最低輪詢頻率
    (Twelve Data = 300s),並於 /health 標示。mock 模式用 mock 本身(開發用)。
    """
    import logging as _logging

    from app.config import get_settings
    s = get_settings()
    if s.mock_data_mode:
        return None  # 主力已是 mock,L1 直接用主力
    if s.capital_api_key and s.capital_identifier and s.capital_api_password:
        try:
            from app.providers.capital_com import CapitalComProvider
            return CapitalComProvider()
        except Exception as exc:  # noqa: BLE001
            _logging.getLogger(__name__).warning("Capital.com 快速報價源不可用: %s", exc)
    if s.oanda_api_token and s.oanda_account_id:
        try:
            from app.providers.oanda import OandaProvider
            return OandaProvider()
        except Exception as exc:  # noqa: BLE001
            _logging.getLogger(__name__).warning("OANDA 快速報價源不可用: %s", exc)
    return None
