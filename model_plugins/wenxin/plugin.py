from monitor_core.plugins import ROOT
from monitor_core.web_plugin import WebModelPlugin


class Plugin(WebModelPlugin):
    id, name, short_name, tone = "wenxin", "文心一言", "文", "wenxin"
    questions = ROOT / "web_collectors" / "questions" / "wenxin.txt"
