"""控制台页面。

三个页面分别对应总览、操作与记录。页面本身只做展示：数据全部通过
``/api/state``、``/api/trend``、``/api/alarms`` 拉取，页面里的按钮走
``/api/batch/start``、``/api/burner/ignite``、``/api/forehearth/gate``、
``/api/mold/blow``、``/api/lehr/reset`` 与 ``/api/cycle``，不内嵌任何业务
逻辑。
"""

from __future__ import annotations

from ..app import Platform

PAGE_TITLES: dict[str, str] = {
    "overview": "总览",
    "operations": "操作",
    "records": "记录查询",
}

_STYLE = """
body { font-family: "Microsoft YaHei", sans-serif; margin: 0; background: #14100d; color: #efe6dc; }
header { padding: 14px 22px; background: #241a14; display: flex; align-items: center; gap: 18px; }
header h1 { font-size: 17px; margin: 0; font-weight: 600; }
nav a { color: #e0a870; margin-right: 14px; text-decoration: none; font-size: 14px; }
nav a.active { color: #ffd479; font-weight: 700; }
main { padding: 20px 22px; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
section { background: #241a14; border-radius: 8px; padding: 14px 16px; }
section h2 { font-size: 14px; margin: 0 0 10px; color: #e6b98a; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
td { padding: 4px 6px; border-bottom: 1px solid #3a2a20; }
td.k { color: #b99d85; width: 42%; }
button { background: #8a5320; color: #fff; border: 0; border-radius: 4px; padding: 7px 14px; margin-right: 8px; cursor: pointer; }
button.warn { background: #8f2f29; }
pre { font-size: 12px; background: #0d0a08; padding: 10px; border-radius: 6px; overflow-x: auto; max-height: 260px; }
"""

_SCRIPT = """
async function refresh() {
  const state = await (await fetch('/api/state')).json();
  const alarms = await (await fetch('/api/alarms')).json();
  const trend = await (await fetch('/api/trend?window_s=600')).json();
  document.getElementById('out-state').textContent = JSON.stringify(state, null, 2);
  document.getElementById('out-alarms').textContent =
    alarms.count + ' 条告警\\n' + JSON.stringify(alarms.alarms.slice(-8), null, 2);
  document.getElementById('out-trend').textContent = JSON.stringify(trend.series, null, 2);
}
async function act(path, payload) {
  const init = {method: 'POST', headers: {'Content-Type': 'application/json'}};
  if (payload) { init.body = JSON.stringify(payload); }
  const result = await (await fetch(path, init)).json();
  document.getElementById('out-action').textContent = JSON.stringify(result, null, 2);
  refresh();
}
window.addEventListener('DOMContentLoaded', () => {
  refresh();
  setInterval(refresh, 5000);
});
"""

_FOCUS = {
    "overview": "液面、窑温、助燃风与炉压的实时状态灯，以及退火告警列表",
    "operations": "投料、点火、开闸板、吹气与退火复位的操作按钮",
    "records": "审计条目、告警历史与落盘日志",
}


def _nav(active: str, pages: tuple[str, ...]) -> str:
    items = []
    for page in pages:
        title = PAGE_TITLES.get(page, page)
        css = ' class="active"' if page == active else ""
        href = "/" if page == "overview" else f"/{page}"
        items.append(f'<a href="{href}"{css}>{title}</a>')
    return "<nav>" + "".join(items) + "</nav>"


def _operations(path: str) -> str:
    if path == "operations":
        return (
            "<section><h2>投料与熔化</h2>"
            "<button onclick=\"act('/api/batch/start', {path: 'start'})\">投料</button>"
            "<button onclick=\"act('/api/batch/start', {path: 'resume'})\">恢复投料</button>"
            "<button onclick=\"act('/api/burner/ignite', {path: 'ignite'})\">点火</button>"
            "<button onclick=\"act('/api/burner/ignite', {path: 'reignite', reason: '控制台'})\">重新点火</button>"
            "<button class='warn' onclick=\"act('/api/burner/release', {reason: '控制台人工确认'})\">点火闩锁复位</button>"
            "<pre id='out-action'>等待操作</pre></section>"
            "<section><h2>供料与成型</h2>"
            "<button onclick=\"act('/api/forehearth/gate', {path: 'warm_up'})\">开闸板</button>"
            "<button onclick=\"act('/api/forehearth/gate', {path: 'reheat'})\">再升温开闸板</button>"
            "<button onclick=\"act('/api/mold/blow', {path: 'cycle'})\">吹气</button>"
            "<button onclick=\"act('/api/lehr/reset', {})\">退火复位</button>"
            "<button onclick=\"act('/api/cycle', {load: 0.85})\">推进周期</button></section>"
            "<section><h2>顺序</h2>"
            "<button onclick=\"act('/api/start')\">启动顺序</button>"
            "<button class='warn' onclick=\"act('/api/stop')\">停炉顺序</button></section>"
        )
    if path == "records":
        return (
            "<section><h2>本页关注</h2>"
            f"<p style='font-size:13px;color:#e6b98a'>{_FOCUS[path]}</p>"
            "<button onclick=\"act('/api/cycle', {load: 0.6})\">采样一次</button>"
            "<pre id='out-action'>等待操作</pre></section>"
            "<section><h2>审计条目</h2><pre id='out-state'>加载中</pre></section>"
            "<section><h2>告警</h2><pre id='out-alarms'>加载中</pre></section>"
            "<section><h2>趋势窗口（10 分钟）</h2><pre id='out-trend'>加载中</pre></section>"
        )
    return (
        "<section><h2>本页关注</h2>"
        f"<p style='font-size:13px;color:#e6b98a'>{_FOCUS[path]}</p>"
        "<button onclick=\"act('/api/cycle', {load: 0.85})\">推进周期</button>"
        "<pre id='out-action'>等待操作</pre></section>"
        "<section><h2>实时状态</h2><pre id='out-state'>加载中</pre></section>"
        "<section><h2>告警列表</h2><pre id='out-alarms'>加载中</pre></section>"
        "<section><h2>趋势窗口（10 分钟）</h2><pre id='out-trend'>加载中</pre></section>"
    )


def render_page(name: str, platform: Platform) -> str:
    """渲染一个控制台页面。"""

    if name not in PAGE_TITLES:
        raise KeyError(f"未知页面: {name}")
    pages = platform.config.pages
    title = PAGE_TITLES[name]
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>{platform.config.name} · {title}</title>"
        f"<style>{_STYLE}</style></head><body>"
        "<header><h1>"
        f"{platform.config.name} 控制台（{platform.config.project_id}）</h1>"
        f"{_nav(name, pages)}</header>"
        f"<main>{_operations(name)}</main>"
        f"<script>{_SCRIPT}</script></body></html>"
    )
