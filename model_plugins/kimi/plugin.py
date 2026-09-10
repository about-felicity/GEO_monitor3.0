from monitor_core.plugins import ROOT
from monitor_core.web_plugin import WebModelPlugin


class Plugin(WebModelPlugin):
    id, name, short_name, tone = "kimi", "Kimi", "K", "kimi"
    questions = ROOT / "web_collectors" / "questions" / "kimi.txt"
