from monitor_core.plugins import ROOT
from monitor_core.web_plugin import WebModelPlugin


class Plugin(WebModelPlugin):
    id, name, short_name, tone = "yuanbao", "腾讯元宝", "元", "yuanbao"
    questions = ROOT / "web_collectors" / "questions" / "yuanbao.txt"
