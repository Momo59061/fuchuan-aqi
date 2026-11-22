"""富川瑶族自治县：天气实况及污染物气象扩散条件预报 文本生成器。

实况：weather.com 过去 24 小时逐小时（含今天已过整点）
预报：weather.com 未来 48 小时逐小时
扩散条件：页面下拉选择（无自动数据源）
"""

import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
import streamlit as st

try:
    import pandas as pd
except ImportError:  # 纯逻辑测试环境可不装 pandas
    pd = None

CN_TZ = ZoneInfo("Asia/Shanghai")
TWC_BASE = "https://api.weather.com"
TWC_PAGE = (
    "https://weather.com/zh-CN/cn/guangxi/hezhou/"
    "fuchuan-yao-autonomous-county/postcode/542700/today"
)
FALLBACK_TWC_KEY = "71f92ea9dd2f4790b92ea9dd2f779061"
DEFAULT_GEOCODE = "24.897,111.324"
MISSING = "【待填】"

HEADER_LINE = "天气实况及污染物气象扩散条件预报"
FOOTER = "（富川瑶族自治县气象台{date}{hour}时）"


class DataSourceError(Exception):
    """外部数据接口不可用。"""


# ---------------------------------------------------------------- 时区 / 格式化

def cn_now() -> datetime:
    """北京时间当前时刻（naive）。"""
    return datetime.now(CN_TZ).replace(tzinfo=None)


def cn_today() -> date:
    """北京时间今天。"""
    return cn_now().date()


def fmt_md(d: date) -> str:
    """日期 → "11月1日"（不补零）。"""
    return f"{d.month}月{d.day}日"


def fmt_num(x) -> str:
    """数值 → 整数或一位小数。"""
    if x is None:
        return MISSING
    x = round(float(x), 1)
    return str(int(x)) if abs(x - round(x)) < 0.05 else f"{x:.1f}"


# ---------------------------------------------------------------- 时段解析

TEMPLATE_LABELS = ["08时模板", "22时模板", "整点模板"]

CONTROL_OPTIONS = [
    "副高控制",
    "高空槽影响",
    "弱冷空气影响",
    "偏南气流控制",
    "低压倒槽影响",
    "冷锋影响",
    "西南暖湿气流控制",
    "手动输入…",
]

OBS_OPTIONS = {
    "08时模板": ["昨天晚上", "今天上午"],
    "22时模板": ["今天20时-22时", "昨天20时-22时"],
}
FCST_OPTIONS = {
    "08时模板": ["今天白天", "今天夜间"],
    "22时模板": ["今天晚上"],
}

RELEASE_HOURS = [8, 10, 12, 14, 16, 18, 20, 22]


def at(d: date, h: int) -> datetime:
    """某日 h 点整；h=24 自动进位到次日 0 点。"""
    return datetime.combine(d, time(hour=0)) + timedelta(hours=h)


def resolve_period(template_kind: str, obs_label: str, fcst_label: str, base: date):
    """返回 (实况起, 实况止, 预报起, 预报止, 正文实况前缀, 页脚日期, 发布时)。"""
    d = base
    if template_kind == "08时模板":
        obs_map = {
            "昨天晚上": (at(d - timedelta(days=1), 20), at(d, 8), f"{fmt_md(d - timedelta(days=1))}晚上"),
            "今天上午": (at(d, 8), at(d, 12), f"{fmt_md(d)}上午"),
        }
        fcst_map = {
            "今天白天": (at(d, 8), at(d, 20)),
            "今天夜间": (at(d, 20), at(d + timedelta(days=1), 8)),
        }
        obs_start, obs_end, obs_prefix = obs_map[obs_label]
        fcst_start, fcst_end = fcst_map[fcst_label]
        return obs_start, obs_end, fcst_start, fcst_end, obs_prefix, d, 8

    if template_kind == "22时模板":
        obs_map = {
            "今天20时-22时": (at(d, 20), at(d, 22), f"{fmt_md(d)}20时-22时"),
            "昨天20时-22时": (
                at(d - timedelta(days=1), 20),
                at(d - timedelta(days=1), 22),
                f"{fmt_md(d - timedelta(days=1))}20时-22时",
            ),
        }
        obs_start, obs_end, obs_prefix = obs_map[obs_label]
        return obs_start, obs_end, at(d, 22), at(d + timedelta(days=1), 8), obs_prefix, d, 22

    # 整点模板：默认实况 = 发布前 2h → 发布时，预报 = 发布时 → 发布后 2h
    release = int(obs_label)
    obs_start, obs_end = at(d, release - 2), at(d, release)
    fcst_start, fcst_end = at(d, release), at(d, release + 2)
    return obs_start, obs_end, fcst_start, fcst_end, "", d, release


def fmt_span(start: datetime, end: datetime) -> str:
    """实况前缀，同模板口径：12 时示例 "11月1日10-12时"；跨午夜写 "11月1日20时-次日08时"。"""
    if end.day != start.day:
        head = "次日" if (end.date() - start.date()).days == 1 else fmt_md(end.date())
        return f"{fmt_md(start.date())}{start.hour}时-{head}{end.hour}时"
    return f"{fmt_md(start.date())}{start.hour}-{end.hour}时"


def fmt_fcst_span(start: datetime, end: datetime) -> str:
    """预报时间前缀，同模板口径；换日时写 "11月1日22时-次日08时"。"""
    if end.day != start.day:
        head = "次日" if (end.date() - start.date()).days == 1 else fmt_md(end.date())
        return f"{fmt_md(start.date())}{start.hour}时-{head}{end.hour}时"
    return f"{fmt_md(start.date())}{start.hour}-{end.hour}时"


def expand_slots(start: datetime, end: datetime) -> list[datetime]:
    """[start, end) 内的全部整点。"""
    hours = int((end - start).total_seconds() // 3600)
    return [start + timedelta(hours=i) for i in range(hours)]


# ---------------------------------------------------------------- 单位换算

def kmh_to_scale(speed):
    """km/h → 蒲福风级。22 km/h 为 4 级。"""
    if speed is None:
        return None
    try:
        v = float(speed)
    except (TypeError, ValueError):
        return None
    bounds = [1, 6, 12, 20, 29, 39, 50, 62, 75, 89, 103, 118]
    return sum(1 for b in bounds if v >= b)


WEATHER_ATOMS = [
    "大暴雨", "暴雨", "大雨", "中雨", "小雨", "雷阵雨", "阵雨", "雨夹雪", "阵雪",
    "小雪", "中雪", "大雪", "雾", "霾", "浮尘", "扬沙", "沙尘", "多云", "阴天", "阴", "晴",
]
TRAD_TO_SIMP = str.maketrans({"雲": "云", "陣": "阵", "陰": "阴", "轉": "转", "強": "强", "風": "风"})

# weather.com iconCode → 规范天气词
TWC_ICON = {
    3: "雷阵雨", 4: "雷阵雨", 37: "雷阵雨", 38: "雷阵雨", 47: "雷阵雨",
    5: "雨夹雪", 6: "雨夹雪", 7: "雨夹雪", 35: "雨夹雪", 44: "雨夹雪",
    9: "小雨", 10: "小雨", 12: "小雨", 40: "大雨",
    11: "阵雨", 39: "阵雨", 45: "阵雨",
    13: "阵雪", 14: "阵雪", 16: "小雪", 41: "阵雪", 42: "大雪", 43: "阵雪", 46: "阵雪",
    15: "阵雪", 17: "雨夹雪", 18: "雨夹雪",
    19: "扬沙", 20: "雾", 21: "霾", 22: "霾",
    26: "阴", 27: "多云", 28: "多云", 29: "多云", 30: "多云",
    31: "晴", 32: "晴", 33: "晴", 34: "晴",
}


def _normalize_dir(cardinal: str) -> str:
    """TWC 方位 → 文案风向："北" → "偏北风"，"西北偏北" → "偏北风"。"""
    if not cardinal:
        return ""
    s = cardinal.translate(TRAD_TO_SIMP).replace("风", "")
    compass = [c for c in s if c in "东南西北"]
    if not compass:
        return s + "风"
    return f"偏{compass[-1]}风"


def _match_atom(s: str) -> str:
    """在句子中匹配规范天气词（长词优先）。"""
    for atom in WEATHER_ATOMS:
        if atom in s:
            return atom
    return ""


def twc_phrase_to_cn(phrase: str, icon=None) -> str:
    """TWC 短语（繁体/简体均可）→ 规范天气词；无法识别时用 iconCode。"""
    p = (phrase or "").translate(TRAD_TO_SIMP)
    for noise in ("大部分地区", "大部地区", "部分地区", "局部地区", "大部", "局部", "部分", "地区"):
        p = p.replace(noise, "")
    if p:
        parts = [x for x in re.split(r"[转至到→]", p) if x]
        mapped = []
        for part in parts:
            atom = _match_atom(part)
            if not atom:
                atom = TWC_ICON.get(_int(icon), "") or part
            mapped.append(atom)
        return "转".join(mapped)
    return TWC_ICON.get(_int(icon), "")


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def atomize(phrase: str) -> list[str]:
    """把 "多云转晴" 等变化句拆成原子天气词（保序）。"""
    if not phrase:
        return []
    parts = [p for p in re.split(r"[转至到→]", phrase) if p]
    found = []
    for p in parts:
        for atom in WEATHER_ATOMS:
            if atom in p:
                found.append(atom)
                break
        else:
            found.append(p)
    # "阴天" 与 "阴" 归一
    return ["阴" if a == "阴天" else a for a in found]


def join_weather(atoms: list[str]) -> str:
    """原子天气词 → 模板口径的短语。"""
    return "".join(atoms)


WEATHER_CATEGORY = {
    "雷阵雨伴有冰雹": 1, "暴雨": 1, "大暴雨": 1, "大雨": 1, "中雨": 1, "雷阵雨": 1,
    "阵雨": 1, "小雨": 1, "雨夹雪": 1, "小雪": 1, "中雪": 1, "大雪": 1, "阵雪": 1,
    "阴": 2, "多云": 2, "雾": 2, "霾": 2, "浮尘": 2, "扬沙": 2, "沙尘": 2,
    "晴": 3,
}


def merge_weather(phrases: list[str]) -> str:
    """整点现象序列 → 合并描述。

    全同 → 单值；首尾同且中间夹他现象 → "{首}有{中间高类别}"；
    首尾不同 → "{首}转{末}"（按首次出现顺序）。
    """
    seq: list[str] = []
    for ph in phrases:
        for a in atomize(ph):
            if not seq or seq[-1] != a:
                seq.append(a)
    if not seq:
        return ""
    if len(set(seq)) == 1:
        return seq[0]
    uniq = list(dict.fromkeys(seq))
    if len(uniq) == 2 and seq[0] == seq[-1]:
        others = [a for a in seq if a != seq[0]]
        if others:
            between = Counter(others).most_common(1)[0][0]
            return f"{seq[0]}有{between}"
    if seq[0] == seq[-1]:
        between = [a for a in seq if a != seq[0]]
        if between:
            between.sort(key=lambda a: WEATHER_CATEGORY.get(a, 9))
            return f"{seq[0]}有{between[0]}"
    if len(uniq) == 2:
        return f"{seq[0]}转{seq[-1]}"
    return f"{seq[0]}转{seq[-1]}"


# ---------------------------------------------------------------- 外部数据

def twc_request(path: str, params: dict, key: str) -> dict:
    """调用 weather.com 公共接口，失败抛 DataSourceError。"""
    q = dict(params)
    q.update({"format": "json", "apiKey": key})
    try:
        r = requests.get(TWC_BASE + path, params=q, timeout=10)
    except requests.RequestException as exc:
        raise DataSourceError(f"weather.com 连接失败：{exc}") from exc
    if r.status_code != 200:
        raise DataSourceError(
            f"weather.com 接口返回 HTTP {r.status_code}：{r.text[:120]}（TWC_KEY 可能已失效，"
            f"可在页面 {TWC_PAGE} 的网页源码中重新搜索 api.weather.com 的 key 并填入 Secrets）"
        )
    return r.json()


def parse_twc_local(s: str) -> datetime:
    """'2026-11-18T16:00:00+0800' → naive 北京时间。"""
    dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    return dt.astimezone(CN_TZ).replace(tzinfo=None)


def hours_from_twc_historical(payload: dict, source: str = "TWC实况") -> list[dict]:
    """TWC 并列数组 → Hour 列表（按时间升序）。"""
    times = payload.get("validTimeLocal") or []
    n = len(times)
    temps = payload.get("temperature") or [None] * n
    hums = payload.get("relativeHumidity") or [None] * n
    cards = payload.get("windDirectionCardinal") or [None] * n
    speeds = payload.get("windSpeed") or [None] * n
    icons = payload.get("iconCode") or [None] * n
    phrases = payload.get("wxPhraseLong") or [""] * n

    out = []
    for i, t in enumerate(times):
        if not t:
            continue
        out.append({
            "time": parse_twc_local(t),
            "temp": _to_num(temps[i]) if i < len(temps) else None,
            "humidity": _to_num(hums[i]) if i < len(hums) else None,
            "wind_dir": _normalize_dir(cards[i]) if i < len(cards) else "",
            "wind_scale": kmh_to_scale(speeds[i]) if i < len(speeds) else None,
            "text": twc_phrase_to_cn(phrases[i] if i < len(phrases) else "",
                                     icons[i] if i < len(icons) else None),
            "source": source,
        })
    out.sort(key=lambda h: h["time"])
    return out


def hours_from_twc_forecast(payload: dict, source: str = "TWC预报") -> list[dict]:
    """TWC 逐小时预报（并列数组）→ Hour 列表。字段与实况接口一致，短语为简体。"""
    return hours_from_twc_historical(payload, source=source)


def _to_num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


AP_OPTIONS = ["一般", "好", "较好", "一般到较差", "较差", "差"]


# ---------------------------------------------------------------- 数据合并 / 聚合

def collect_hours(start: datetime, end: datetime,
                  twc_history_hours: list[dict], twc_forecast_hours: list[dict]):
    """窗口 [start, end) 的整点数据：实况优先，实况没有的整点取 TWC 预报。"""
    hist_map = {h["time"]: h for h in twc_history_hours}
    fc_map = {h["time"]: h for h in twc_forecast_hours}

    rows, missing = [], []
    for slot in expand_slots(start, end):
        src = hist_map.get(slot) or fc_map.get(slot)
        if src is None:
            rows.append({"time": slot, "temp": None, "humidity": None, "wind_dir": "",
                         "wind_scale": None, "text": "", "source": MISSING, "missing": True})
            missing.append(slot)
        else:
            r = dict(src)
            r["missing"] = False
            rows.append(r)
    return rows, missing


def aggregate(rows: list[dict]) -> dict:
    """整点行 → 模板字段区间。"""
    temps = [r["temp"] for r in rows if r.get("temp") is not None]
    hums = [r["humidity"] for r in rows if r.get("humidity") is not None]
    scales = [r["wind_scale"] for r in rows if r.get("wind_scale") is not None]
    dirs = [r["wind_dir"] for r in rows if r.get("wind_dir")]
    texts = [r["text"] for r in rows if r.get("text")]

    def rng(values, fmt):
        lo, hi = fmt(min(values)), fmt(max(values))
        return lo if lo == hi else f"{lo}-{hi}"

    return {
        "temp": rng(temps, fmt_num) if temps else MISSING,
        "humidity": rng(hums, fmt_num) if hums else MISSING,
        "scale": (str(min(scales)) if min(scales) == max(scales)
                   else f"{min(scales)}-{max(scales)}") if scales else MISSING,
        "wind_dir": Counter(dirs).most_common(1)[0][0] if dirs else MISSING,
        "weather": merge_weather(texts) or MISSING,
        "n": len(temps),
    }


# ---------------------------------------------------------------- 模板拼装

def build_bulletin(template_kind: str, obs: dict, fcst: dict, control: str,
                   ap: str, base: date, obs_label: str = "", fcst_label: str = "",
                   window: tuple | None = None) -> str:
    """按模板拼装文案；缺字段用 【待填】。window=(实况起, 实况止, 预报起, 预报止, 发布时)。"""
    a, fa = obs["scale"], fcst["scale"]
    if template_kind == "08时模板":
        if obs_label == "今天上午":
            obs_prefix = f"{fmt_md(base)}上午"
        else:
            obs_prefix = f"{fmt_md(base - timedelta(days=1))}晚上"
        fcst_clause = fcst_label or "今天白天"
        lines = [
            f"{HEADER_LINE}",
            f"{obs_prefix}，我县受{control}，{obs['weather']}，气温{obs['temp']}℃，"
            f"相对湿度{obs['humidity']}%，地面主导风为{obs['wind_dir']}{a}级，"
            f"大气污染物气象扩散条件{ap}。",
            f"经最新气象资料分析，{fcst_clause}我县继续受{control}，{fcst['weather']}，"
            f"地面主导风为{fcst['wind_dir']}{fa}级，气温{fcst['temp']}℃，"
            f"相对湿度{fcst['humidity']}%，大气污染物气象扩散条件{ap}。",
            f"（富川瑶族自治县气象台{fmt_md(base)}08时）",
        ]
        return "\n".join(lines)

    if template_kind == "22时模板":
        obs_date = fmt_md(base)
        lines = [
            f"{HEADER_LINE}",
            f"{obs_date}20时-22时，我县受{control}，{obs['weather']}，气温{obs['temp']}℃，"
            f"相对湿度{obs['humidity']}%，地面主导风为{obs['wind_dir']}{a}级，"
            f"大气污染物气象扩散条件{ap}。",
            f"经最新气象资料分析，今天晚上我县继续受{control}，{fcst['weather']}，"
            f"地面主导风为{fcst['wind_dir']}{fa}级，气温{fcst['temp']}℃，"
            f"相对湿度{fcst['humidity']}%，大气污染物气象扩散条件{ap}。",
            f"（富川瑶族自治县气象台{obs_date}22时）",
        ]
        return "\n".join(lines)

    obs_start, obs_end, fcst_start, fcst_end, release = window
    lines = [
        f"{HEADER_LINE}",
        f"{fmt_span(obs_start, obs_end)}，我县受{control}，{obs['weather']}，气温{obs['temp']}℃，"
        f"相对湿度{obs['humidity']}%，地面主导风为{obs['wind_dir']}{a}级，"
        f"大气污染物气象扩散条件{ap}。",
        f"经最新气象资料分析，{fmt_fcst_span(fcst_start, fcst_end)}我县继续受{control}，{fcst['weather']}，"
        f"地面主导风为{fcst['wind_dir']}{fa}级，气温{fcst['temp']}℃，"
        f"相对湿度{fcst['humidity']}%，大气污染物气象扩散条件{ap}。",
        FOOTER.format(date=fmt_md(obs_start.date()), hour=release),
    ]
    return "\n".join(lines)


def resolve_window(kind: str, base: date, release: int, obs_label: str, fcst_label: str,
                   custom_obs_start: datetime | None, custom_fcst_end: datetime | None):
    """返回 (实况起, 实况止, 预报起, 预报止, 发布时)。"""
    if kind != "整点模板":
        s0, e0, s1, e1, _, _, hour = resolve_period(kind, obs_label, fcst_label, base)
        return s0, e0, s1, e1, hour
    obs_start = custom_obs_start if custom_obs_start else at(base, release - 2)
    fcst_end = custom_fcst_end if custom_fcst_end else at(base, release + 2)
    obs_end = fcst_start = at(base, release)
    if obs_start >= obs_end:
        obs_start = obs_end - timedelta(hours=2)
    if fcst_end <= fcst_start:
        fcst_end = fcst_start + timedelta(hours=2)
    return obs_start, obs_end, fcst_start, fcst_end, release


# ---------------------------------------------------------------- Streamlit 界面

def get_secrets():
    """读取 weather.com 相关 Secrets（均有默认值，缺省也能运行）。"""
    try:
        twc_key = str(st.secrets["TWC_KEY"]).strip() or FALLBACK_TWC_KEY
    except Exception:
        twc_key = FALLBACK_TWC_KEY
    try:
        geocode = str(st.secrets["TWC_GEOCODE"]).strip() or DEFAULT_GEOCODE
    except Exception:
        geocode = DEFAULT_GEOCODE
    return twc_key, geocode


@st.cache_data(ttl=600, show_spinner="拉取 weather.com 实况…")
def load_twc_history(twc_key: str, geocode: str) -> list[dict]:
    """过去 24 小时逐小时实况。"""
    payload = twc_request(
        "/v3/wx/conditions/historical/hourly/1day",
        {"geocode": geocode, "units": "m", "language": "zh-CN", "par": 0},
        twc_key,
    )
    return hours_from_twc_historical(payload)


@st.cache_data(ttl=600, show_spinner="拉取 weather.com 48 小时预报…")
def load_twc_forecast(twc_key: str, geocode: str) -> list[dict]:
    """未来 48 小时逐小时预报。"""
    payload = twc_request(
        "/v3/wx/forecast/hourly/2day",
        {"geocode": geocode, "units": "m", "language": "zh-CN"},
        twc_key,
    )
    return hours_from_twc_forecast(payload)


def render_copy_button() -> None:
    """复制按钮：读取当前 textarea 的值写入剪贴板，并同步复制隐藏输入框里的内容兜底。"""
    html = r"""
<div id="fcwrap" style="margin-top:0.4rem">
<button id="fccopy" style="width:100%;padding:0.55rem;border:0;border-radius:0.5rem;
background:#0f766e;color:#fff;font-size:1rem;cursor:pointer">复制全文</button>
<textarea id="fcsrc" aria-hidden="true" tabindex="-1"
 style="position:absolute;left:-9999px;top:0;width:1px;height:1px;opacity:0"></textarea>
</div>
<script>
(function(){
  var b = document.getElementById('fccopy');
  if (!b || b.dataset.bound) return;
  b.dataset.bound = '1';
  var src = document.getElementById('fcsrc');
  var base = '#0f766e';
  function reset(){ b.textContent='复制全文'; b.style.background=base; }
  function ok(){ b.textContent='已复制'; b.style.background='#16a34a'; setTimeout(reset, 1500); }
  function bad(){ b.textContent='复制失败，请手动选择复制'; b.style.background='#dc2626'; setTimeout(reset, 2500); }
  function findTextArea(){
    var areas = Array.prototype.slice.call(document.querySelectorAll('div[data-testid="stTextArea"] textarea'));
    var visible = areas.filter(function(t){ return t.offsetParent !== null; });
    return visible.length ? visible[visible.length-1] : areas[areas.length-1];
  }
  b.onclick = function(){
    var ta = findTextArea();
    if (!ta) { bad(); return; }
    var text = ta.value;
    // 先把最新文本写进隐藏框并选中，execCommand 就会复制它（Windows/Chrome 上更可靠）
    src.value = text;
    src.focus();
    src.select();
    var done = false;
    try { done = document.execCommand('copy'); } catch(e) { done = false; }
    if (done) { ok(); return; }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(ok, bad);
    } else {
      bad();
    }
  };
})();
</script>
"""
    try:
        st.html(html, unsafe_allow_javascript=True)
    except TypeError:
        import streamlit.components.v1 as components

        components.html(html, height=72)


def signature(kind: str, base: date, obs_label: str, fcst_label: str, control: str,
              release: int, obs_start: datetime, obs_end: datetime,
              fcst_start: datetime, fcst_end: datetime) -> str:
    return "|".join([
        kind, base.isoformat(), obs_label, fcst_label, control, str(release),
        f"{obs_start:%Y%m%d%H}", f"{obs_end:%Y%m%d%H}",
        f"{fcst_start:%Y%m%d%H}", f"{fcst_end:%Y%m%d%H}",
    ])


def main() -> None:
    st.set_page_config(page_title="天气实况及污染物气象扩散条件预报", page_icon="🌤", layout="centered")
    st.header("天气实况及污染物气象扩散条件预报")

    twc_key, geocode = get_secrets()
    now = cn_now()
    today = now.date()

    kind = st.selectbox("模板", TEMPLATE_LABELS, key="kind")
    base = st.date_input("基准日期", value=today,
                         min_value=today - timedelta(days=9), max_value=today, key="base")
    control_choice = st.selectbox("控制系统", CONTROL_OPTIONS, key="control_choice")
    if control_choice == "手动输入…":
        control = st.text_input("控制系统（手动）", placeholder="例如：副高控制", key="control_text").strip()
    else:
        control = control_choice

    release = 8
    custom_obs_start = custom_fcst_end = None
    obs_opts, fcst_opts = [], []
    if kind == "整点模板":
        release = st.selectbox("发布时次（整点）", RELEASE_HOURS, index=2, key="release")
        with st.expander("自定义时段（默认：实况=发布前2小时，预报=发布后2小时）"):
            obs_start_date = st.date_input(
                "实况起点日期", value=base, min_value=today - timedelta(days=9),
                max_value=today, key="cos_date")
            obs_start_hour = st.selectbox("实况起点时", range(24),
                                          index=max(0, release - 2), key="cos_hour")
            fcst_end_date = st.date_input(
                "预报终点日期", value=base, min_value=today - timedelta(days=9),
                max_value=today + timedelta(days=1), key="cfe_date")
            fcst_end_hour = st.selectbox("预报终点时", range(24),
                                         index=min(23, release + 2), key="cfe_hour")
        custom_obs_start = at(obs_start_date, obs_start_hour)
        custom_fcst_end = at(fcst_end_date, fcst_end_hour)
    else:
        obs_opts = OBS_OPTIONS[kind]
        fcst_opts = FCST_OPTIONS[kind]

    ap = st.selectbox("大气污染物气象扩散条件", AP_OPTIONS, key="ap")

    with st.form("generate"):
        obs_label = obs_opts[0] if obs_opts else ""
        fcst_label = fcst_opts[0] if fcst_opts else ""
        if kind != "整点模板":
            obs_label = st.selectbox("实况时段", obs_opts, key=f"obs_label_{kind}")
            fcst_label = st.selectbox("预报时段", fcst_opts, key=f"fcst_label_{kind}")
        else:
            st.caption("实况/预报时段可在上方“自定义时段”中调整。")
        submitted = st.form_submit_button("生成文本", type="primary", use_container_width=True)

    obs_start, obs_end, fcst_start, fcst_end, release_hour = resolve_window(
        kind, base, release, obs_label, fcst_label, custom_obs_start, custom_fcst_end)
    st.caption(
        f"实况：{obs_start:%m-%d %H:%M} → {obs_end:%m-%d %H:%M}　|　"
        f"预报：{fcst_start:%m-%d %H:%M} → {fcst_end:%m-%d %H:%M}"
    )

    sig = signature(kind, base, obs_label, fcst_label, control, release,
                    obs_start, obs_end, fcst_start, fcst_end)

    if submitted:
        if not control:
            st.error("请先填写控制系统。")
        else:
            # 各数据源独立降级：任一失败都继续生成，缺的整点在表中【待填】。
            source_errors = []
            twc_obs, twc_fc = [], []
            try:
                twc_obs = load_twc_history(twc_key, geocode)
            except Exception as exc:  # noqa: BLE001
                source_errors.append(f"weather.com 实况：{exc}")
            try:
                twc_fc = load_twc_forecast(twc_key, geocode)
            except Exception as exc:  # noqa: BLE001
                source_errors.append(f"weather.com 预报：{exc}")
            obs_rows, obs_missing = collect_hours(obs_start, obs_end, twc_obs, twc_fc)
            fcst_rows, fcst_missing = collect_hours(fcst_start, fcst_end, twc_obs, twc_fc)
            window = (obs_start, obs_end, fcst_start, fcst_end, release_hour)
            try:
                text = build_bulletin(kind, aggregate(obs_rows), aggregate(fcst_rows),
                                      control, ap, base, obs_label, fcst_label, window)
            except Exception as exc:  # noqa: BLE001
                st.error(f"生成失败：{exc}")
                text = None
            if text is not None:
                st.session_state.update({
                    "bulletin_text": text, "sig": sig, "obs_rows": obs_rows,
                    "fcst_rows": fcst_rows, "obs_missing": obs_missing,
                    "fcst_missing": fcst_missing, "generated": True,
                    "source_errors": source_errors, "last_ap": ap,
                    "gen_args": dict(kind=kind, base=base, obs_label=obs_label,
                                     fcst_label=fcst_label, control=control, window=window),
                })

    if not st.session_state.get("generated"):
        st.text_area("通报文本", key="bulletin_text", height=320,
                     placeholder="生成后在此显示，可直接编辑")
        return

    for err in st.session_state.get("source_errors") or []:
        st.error(f"数据源不可用：{err}（相应整点显示【待填】，可在表格或文本中手工补录）")

    # 扩散条件改了：用缓存的整点数据重算文本（不联网）；会覆盖文本框里的手工修改。
    if st.session_state.get("generated") and ap != st.session_state.get("last_ap"):
        args = st.session_state.get("gen_args") or {}
        st.session_state["bulletin_text"] = build_bulletin(
            args.get("kind", kind), aggregate(st.session_state.get("obs_rows", [])),
            aggregate(st.session_state.get("fcst_rows", [])),
            args.get("control", ""), ap, args.get("base", today),
            args.get("obs_label", ""), args.get("fcst_label", ""),
            args.get("window"))
        st.session_state["last_ap"] = ap
    miss = []
    if st.session_state.get("obs_missing"):
        miss.append("实况缺失 " + "、".join(f"{t:%H}时" for t in st.session_state["obs_missing"]))
    if st.session_state.get("fcst_missing"):
        miss.append("预报缺失 " + "、".join(f"{t:%H}时" for t in st.session_state["fcst_missing"]))
    if miss:
        st.warning("；".join(miss) + "。已在文本或表格中以【待填】占位，补录后自动重算。")
    if base != today:
        st.caption("基准日不是今天：预报为 weather.com 当前时刻起的 48 小时预报，与本时段可能不重合，请人工核对。")

    with st.expander("整点数据（可编辑补录，修改后自动重新统计）", expanded=bool(miss)):
        if pd is None:
            st.error("缺少 pandas，无法显示数据表。")
        else:
            table = pd.DataFrame([
                {
                    "时段": label,
                    "时间": f"{r['time']:%m-%d %H}时",
                    "温度℃": r.get("temp"),
                    "湿度%": r.get("humidity"),
                    "风向": r.get("wind_dir") or "",
                    "风力": r.get("wind_scale"),
                    "天气现象": r.get("text") or "",
                    "来源": r.get("source") or MISSING,
                }
                for label, rows in (("实况", st.session_state.get("obs_rows", [])),
                                    ("预报", st.session_state.get("fcst_rows", [])))
                for r in rows
            ])
            edited = st.data_editor(
                table,
                key=f"editor_{st.session_state.get('sig', '')}",
                hide_index=True,
                use_container_width=True,
                disabled=["时段", "时间", "来源"],
                column_config={
                    "温度℃": st.column_config.NumberColumn(format="%.1f"),
                    "湿度%": st.column_config.NumberColumn(format="%.1f"),
                    "风力": st.column_config.NumberColumn(min_value=0, max_value=12, step=1),
                },
            )
            if not edited.equals(table):
                args = st.session_state.get("gen_args") or {}
                obs_rows2, fcst_rows2 = _rows_from_editor(edited)
                st.session_state["obs_rows"] = obs_rows2
                st.session_state["fcst_rows"] = fcst_rows2
                st.session_state["bulletin_text"] = build_bulletin(
                    args.get("kind", kind), aggregate(obs_rows2), aggregate(fcst_rows2),
                    args.get("control", control), ap,
                    args.get("base", base), args.get("obs_label", ""),
                    args.get("fcst_label", ""), args.get("window"))
                st.rerun()

    st.text_area("通报文本", key="bulletin_text", height=320)
    render_copy_button()
    st.download_button(
        "下载 .txt",
        data=st.session_state.get("bulletin_text", "").encode("utf-8"),
        file_name=f"富川扩散条件预报_{base:%Y%m%d}.txt",
        mime="text/plain",
        use_container_width=True,
    )
    if st.button("刷新数据（清缓存）", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


def _rows_from_editor(df) -> tuple[list[dict], list[dict]]:
    """data_editor 表格 → (实况行, 预报行)；时间列 "11-18 08时" 还原为 datetime。"""
    obs_rows, fcst_rows = [], []
    year = cn_today().year
    for _, row in df.iterrows():
        m = re.match(r"(\d{2})-(\d{2}) (\d{2})时", str(row.get("时间") or ""))
        if not m:
            continue
        stamp = at(date(year, int(m.group(1)), int(m.group(2))), int(m.group(3)))
        raw_scale = row.get("风力")
        if raw_scale is None or (pd is not None and pd.isna(raw_scale)) or str(raw_scale).strip() == "":
            scale = None
        else:
            scale = int(float(raw_scale))
        rec = {
            "time": stamp,
            "temp": _to_num(row.get("温度℃")),
            "humidity": _to_num(row.get("湿度%")),
            "wind_dir": row.get("风向") or "",
            "wind_scale": scale,
            "text": row.get("天气现象") or "",
            "source": row.get("来源") or MISSING,
        }
        (obs_rows if row.get("时段") == "实况" else fcst_rows).append(rec)
    return obs_rows, fcst_rows


if __name__ == "__main__":
    main()
