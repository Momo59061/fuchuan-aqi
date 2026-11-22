"""富川气象文本生成器 —— 纯函数测试（无需网络、无需 Streamlit 运行时）。"""

from datetime import date, datetime

import pytest

import app


def dt(y, m, d, h):
    return datetime(y, m, d, h)


# ---------------------------------------------------------------- 格式化 / 时段

def test_fmt_num():
    assert app.fmt_num(24.0) == "24"
    assert app.fmt_num(26.6) == "26.6"
    assert app.fmt_num(22.8) == "22.8"
    assert app.fmt_num(-0.04) == "0"
    assert app.fmt_num(None) == app.MISSING


def test_fmt_md():
    assert app.fmt_md(date(2026, 1, 5)) == "1月5日"
    assert app.fmt_md(date(2026, 11, 1)) == "11月1日"


def test_resolve_period_08():
    base = date(2026, 11, 18)
    s, e, fs, fe, prefix, issue, hour = app.resolve_period("08时模板", "昨天晚上", "今天白天", base)
    assert (s, e) == (dt(2026, 11, 17, 20), dt(2026, 11, 18, 8))
    assert (fs, fe) == (dt(2026, 11, 18, 8), dt(2026, 11, 18, 20))
    assert prefix == "11月17日晚上"
    assert issue == base and hour == 8


def test_resolve_period_22():
    base = date(2026, 11, 18)
    s, e, fs, fe, _, _, hour = app.resolve_period("22时模板", "今天20时-22时", "今天晚上", base)
    assert (s, e) == (dt(2026, 11, 18, 20), dt(2026, 11, 18, 22))
    assert (fs, fe) == (dt(2026, 11, 18, 22), dt(2026, 11, 19, 8))
    assert hour == 22


def test_resolve_period_cross_year():
    base = date(2027, 1, 1)
    s, e, _, _, prefix, issue, _ = app.resolve_period("08时模板", "昨天晚上", "今天白天", base)
    assert s == dt(2026, 12, 31, 20)
    assert prefix == "12月31日晚上"
    assert issue == date(2027, 1, 1)


def test_fmt_spans():
    assert app.fmt_span(dt(2026, 11, 1, 10), dt(2026, 11, 1, 12)) == "11月1日10-12时"
    assert app.fmt_fcst_span(dt(2026, 11, 1, 12), dt(2026, 11, 1, 14)) == "11月1日12-14时"
    assert app.fmt_span(dt(2026, 11, 1, 20), dt(2026, 11, 2, 8)) == "11月1日20时-次日8时"


# ---------------------------------------------------------------- 单位换算

def test_kmh_to_scale():
    assert app.kmh_to_scale(4) == 1
    assert app.kmh_to_scale(6) == 2
    assert app.kmh_to_scale(22) == 4
    assert app.kmh_to_scale(0) == 0
    assert app.kmh_to_scale(None) is None


def test_normalize_dir():
    assert app._normalize_dir("北") == "偏北风"
    assert app._normalize_dir("西南") == "偏南风"
    assert app._normalize_dir("西") == "偏西风"
    assert app._normalize_dir("") == ""


def test_twc_phrase():
    assert app.twc_phrase_to_cn("大部多雲") == "多云"
    assert app.twc_phrase_to_cn("陣雨") == "阵雨"
    assert app.twc_phrase_to_cn("雨", 12) == "小雨"
    assert app.twc_phrase_to_cn("", 26) == "阴"
    assert app.twc_phrase_to_cn("大部分地区多云") == "多云"


def test_atomize():
    assert app.atomize("多云转晴") == ["多云", "晴"]
    assert app.atomize("阴天") == ["阴"]


# ---------------------------------------------------------------- 现象合并

def test_merge_weather_same():
    assert app.merge_weather(["多云", "多云", "多云"]) == "多云"


def test_merge_weather_middle():
    assert app.merge_weather(["多云", "阵雨", "多云"]) == "多云有阵雨"
    assert app.merge_weather(["多云", "晴", "多云"]) == "多云有晴"
    assert app.merge_weather(["阵雨", "阴", "阵雨"]) == "阵雨有阴"


def test_merge_weather_transition():
    assert app.merge_weather(["小雨", "多云"]) == "小雨转多云"
    assert app.merge_weather(["多云转晴", "晴"]) == "多云转晴"


def test_merge_weather_empty():
    assert app.merge_weather([]) == ""


# ---------------------------------------------------------------- 聚合

def _row(t, temp, hum, d, s, text, src="TWC实况"):
    return {"time": t, "temp": temp, "humidity": hum, "wind_dir": d,
            "wind_scale": s, "text": text, "source": src}


def test_aggregate():
    rows = [
        _row(dt(2026, 11, 18, 8), 22.8, 66, "偏南风", 1, "多云"),
        _row(dt(2026, 11, 18, 9), 26.6, 93, "偏南风", 2, "阵雨"),
        _row(dt(2026, 11, 18, 10), 24.0, 70, "偏北风", 3, "多云"),
    ]
    agg = app.aggregate(rows)
    assert agg["temp"] == "22.8-26.6"
    assert agg["humidity"] == "66-93"
    assert agg["scale"] == "1-3"
    assert agg["wind_dir"] == "偏南风"
    assert agg["weather"] == "多云有阵雨"


def test_aggregate_empty():
    agg = app.aggregate([])
    assert agg["temp"] == agg["humidity"] == agg["scale"] == app.MISSING
    assert agg["wind_dir"] == agg["weather"] == app.MISSING


# ---------------------------------------------------------------- 数据合并

def test_collect_hours_priority():
    twc = [_row(dt(2026, 11, 18, 8), 20, 80, "偏北风", 3, "多云"),
           _row(dt(2026, 11, 18, 9), 21, 79, "偏北风", 3, "多云")]
    fc = [_row(dt(2026, 11, 18, 9), 99, 99, "偏南风", 1, "晴", "TWC预报"),
          _row(dt(2026, 11, 18, 10), 23, 70, "偏南风", 2, "晴", "TWC预报")]
    rows, missing = app.collect_hours(dt(2026, 11, 18, 8), dt(2026, 11, 18, 12), twc, fc)
    assert [r["source"] for r in rows] == ["TWC实况", "TWC实况", "TWC预报", app.MISSING]
    assert rows[1]["temp"] == 21  # 实况优先于预报
    assert missing == [dt(2026, 11, 18, 11)]


def test_twc_forecast_parse():
    payload = {
        "validTimeLocal": ["2026-11-18T12:00:00+0800", "2026-11-18T13:00:00+0800"],
        "temperature": [15, 16],
        "relativeHumidity": [63, 61],
        "windDirectionCardinal": ["西北偏北", "北"],
        "windSpeed": [22, 26],
        "iconCode": [29, 28],
        "wxPhraseLong": ["局部多云", "大部分地区多云"],
    }
    rows = app.hours_from_twc_forecast(payload)
    assert [r["source"] for r in rows] == ["TWC预报", "TWC预报"]
    assert rows[0]["wind_dir"] == "偏北风"
    assert rows[0]["wind_scale"] == 4
    assert rows[0]["text"] == "多云"


def test_ap_options():
    assert "一般" in app.AP_OPTIONS
    assert "一般到较差" in app.AP_OPTIONS
    empty = app.aggregate([])
    text = app.build_bulletin("08时模板", empty, empty, "副高控制", "一般到较差", date(2026, 11, 18))
    assert text.count("一般到较差") == 2


# ---------------------------------------------------------------- 模板逐字符比对

def test_build_bulletin_08_matches_example():
    obs = {"temp": "22.8-26.6", "humidity": "66-93", "scale": "1-2",
           "wind_dir": "偏南风", "weather": "阴天有分散阵雨"}
    fcst = {"temp": "24-30", "humidity": "60-98", "scale": "1-3",
            "wind_dir": "偏南风", "weather": "阴天到多云"}
    expected = (
        "天气实况及污染物气象扩散条件预报\n"
        "10月31日晚上，我县受副高控制，阴天有分散阵雨，气温22.8-26.6℃，相对湿度66-93%，"
        "地面主导风为偏南风1-2级，大气污染物气象扩散条件一般。\n"
        "经最新气象资料分析，今天白天我县继续受副高控制，阴天到多云，地面主导风为偏南风1-3级，"
        "气温24-30℃，相对湿度60-98%，大气污染物气象扩散条件一般。\n"
        "（富川瑶族自治县气象台11月1日08时）"
    )
    assert app.build_bulletin("08时模板", obs, fcst, "副高控制", "一般", date(2025, 11, 1)) == expected


def test_build_bulletin_22_matches_example():
    obs = {"temp": "26.2-26.7", "humidity": "65-69", "scale": "2-3",
           "wind_dir": "偏南风", "weather": "多云"}
    fcst = {"temp": "22-25", "humidity": "75-95", "scale": "2-3",
            "wind_dir": "偏南风", "weather": "多云到阴天"}
    expected = (
        "天气实况及污染物气象扩散条件预报\n"
        "10月31日20时-22时，我县受副高控制，多云，气温26.2-26.7℃，相对湿度65-69%，"
        "地面主导风为偏南风2-3级，大气污染物气象扩散条件一般到较差。\n"
        "经最新气象资料分析，今天晚上我县继续受副高控制，多云到阴天，地面主导风为偏南风2-3级，"
        "气温22-25℃，相对湿度75-95%，大气污染物气象扩散条件一般到较差。\n"
        "（富川瑶族自治县气象台10月31日22时）"
    )
    assert app.build_bulletin("22时模板", obs, fcst, "副高控制", "一般到较差", date(2025, 10, 31)) == expected


def test_build_bulletin_hourly_matches_example():
    obs = {"temp": "24.8-27.9", "humidity": "64-73", "scale": "1-2",
           "wind_dir": "偏北风", "weather": "多云转晴"}
    fcst = {"temp": "25-28", "humidity": "55-65", "scale": "2-3",
            "wind_dir": "偏北风", "weather": "晴到多云"}
    expected = (
        "天气实况及污染物气象扩散条件预报\n"
        "11月1日10-12时，我县受副高控制，多云转晴，气温24.8-27.9℃，相对湿度64-73%，"
        "地面主导风为偏北风1-2级，大气污染物气象扩散条件一般。\n"
        "经最新气象资料分析，11月1日12-14时我县继续受副高控制，晴到多云，地面主导风为偏北风2-3级，"
        "气温25-28℃，相对湿度55-65%，大气污染物气象扩散条件一般。\n"
        "（富川瑶族自治县气象台11月1日12时）"
    )
    window = (dt(2025, 11, 1, 10), dt(2025, 11, 1, 12), dt(2025, 11, 1, 12), dt(2025, 11, 1, 14), 12)
    assert app.build_bulletin("整点模板", obs, fcst, "副高控制", "一般",
                              date(2025, 11, 1), "", "", window) == expected


def test_build_bulletin_missing_fields():
    empty = app.aggregate([])
    text = app.build_bulletin("08时模板", empty, empty, "副高控制", app.MISSING, date(2026, 11, 18))
    assert text.count(app.MISSING) == 12
    assert "气温【待填】℃" in text
