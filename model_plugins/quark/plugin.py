from monitor_core.plugins import ROOT
from monitor_core.web_plugin import WebModelPlugin


class Plugin(WebModelPlugin):
    id, name, short_name, tone = "quark", "夸克", "夸", "quark"
    questions = ROOT / "web_collectors" / "questions" / "quark.txt"
