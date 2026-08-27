from monitor_core.plugins import ROOT
from monitor_core.web_plugin import WebModelPlugin


class Plugin(WebModelPlugin):
    id, name, short_name, tone = "doubao", "豆包", "豆", "doubao"
    questions = ROOT / "web_collectors" / "questions" / "doubao.txt"
