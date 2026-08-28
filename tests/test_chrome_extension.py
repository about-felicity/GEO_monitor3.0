import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "geo_chrome_extension"


class ChromeExtensionTests(unittest.TestCase):
    def test_manifest_covers_three_models_and_server(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        matches = manifest["content_scripts"][0]["matches"]
        self.assertTrue(any("doubao.com" in item for item in matches))
        self.assertTrue(any("yuanbao.tencent.com" in item for item in matches))
        self.assertTrue(any("wenxin.baidu.com" in item for item in matches))
        self.assertTrue(any("ifbcy.com" in item for item in manifest["host_permissions"]))
        self.assertIn("content.css", manifest["content_scripts"][0]["css"])
        self.assertEqual(manifest["options_page"], "popup.html")
        self.assertNotIn("debugger", manifest["permissions"])

    def test_extension_does_not_read_or_persist_cookies(self):
        source = "\n".join(
            (EXTENSION / name).read_text(encoding="utf-8")
            for name in ("content.js", "service-worker.js", "popup.js")
        )
        self.assertNotIn("chrome.cookies", source)
        self.assertNotIn("document.cookie", source)
        self.assertNotIn("localStorage", source)
        self.assertIn("storage.session", source)

    def test_worker_config_is_remembered_locally_and_can_be_cleared(self):
        worker = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        popup = (EXTENSION / "popup.js").read_text(encoding="utf-8")
        self.assertIn('chrome.storage.local.set({ savedConfig: config, autoStart: true })', worker)
        self.assertIn('chrome.storage.local.get(["savedConfig", "autoStart"])', worker)
        self.assertIn("GEO_FORGET_CONFIG", worker)
        self.assertIn("GEO_FORGET_CONFIG", popup)
        self.assertIn("hasConfig: Boolean(value.config?.token)", worker)
        self.assertIn("if (sender.tab)", worker)

    def test_scrapling_style_adaptive_element_relocation_is_local_and_bounded(self):
        content = (EXTENSION / "content.js").read_text(encoding="utf-8")
        worker = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("geoAdaptiveElementsV1", worker)
        self.assertIn("TRUSTED_CONTEXTS", worker)
        self.assertIn("GEO_SAVE_ADAPTIVE_ELEMENT", content)
        self.assertIn("elementFingerprint", content)
        self.assertIn("adaptiveScore", content)
        self.assertIn("relocateAdaptive", content)
        self.assertIn("question-input", content)
        self.assertIn("new-conversation", content)
        self.assertIn("answer-body", content)
        self.assertIn(".slice(0, 1200)", content)

    def test_worker_protocol_uploads_progress_and_results(self):
        source = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("/api/worker/claim", source)
        self.assertIn("/heartbeat", source)
        self.assertIn("/result", source)
        self.assertIn("/finish", source)
        self.assertIn("chrome_extension_logged_in_tab", source)

    def test_model_pages_receive_live_control_panel(self):
        source = (EXTENSION / "content.js").read_text(encoding="utf-8")
        self.assertIn("__geo_monitor_control_panel", source)
        self.assertIn("GEO_PAGE_STATUS", source)
        self.assertIn("GEO_STATUS", source)
        self.assertIn("任务连接", source)
        self.assertIn("geo-logs", source)
        self.assertIn("设置 Worker 密钥并连接", source)
        self.assertIn("任务管理", source)
        self.assertIn("element.closest(`#${PANEL_ID}`)", source)
        self.assertIn("setInterval(syncPanelAndPoll, 5000)", source)
        self.assertIn("已停止 · 配置已保存", source)
        self.assertIn("尚未设置 Worker", source)
        self.assertNotIn('renderPanel({ running: true, status: message.status || {} })', source)

    def test_existing_model_tabs_are_injected_after_extension_reload(self):
        source = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("injectKnownTabs", source)
        self.assertIn("chrome.scripting.insertCSS", source)
        self.assertIn("chrome.tabs.onUpdated.addListener", source)
        self.assertIn("chrome.runtime.openOptionsPage", source)
        self.assertIn("GEO_OPEN_TASKS", source)
        self.assertIn("monitorTaskKey", source)
        self.assertIn("openTaskManager", source)
        self.assertIn("chrome.scripting.executeScript", source)

    def test_worker_runs_one_task_before_polling_for_the_next(self):
        source = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("if (polling || executing) return", source)
        self.assertIn("await runTask(state.config, response.task)", source)
        self.assertIn("setTimeout(poll, 5000)", source)

    def test_worker_resumes_after_extension_refresh_and_skips_finished_rounds(self):
        source = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("task.completed_rounds?.[model]", source)
        self.assertIn("completedRounds.has(roundNumber)", source)
        self.assertIn("task.model_progress?.[model]?.completed", source)
        self.assertIn("progress.pause_requested", source)
        self.assertIn("uploaded.pause_requested", source)
        self.assertIn('finish(config, task, "paused")', source)

    def test_three_models_run_in_parallel_and_analysis_is_local(self):
        source = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("Promise.allSettled", source)
        self.assertIn("models.map((model) => runModel", source)
        self.assertIn("analyzeLocally", source)
        self.assertIn('mode: "local_chrome_extension"', source)

    def test_each_answer_collects_all_visible_source_links(self):
        source = (EXTENSION / "content.js").read_text(encoding="utf-8")
        self.assertIn("unwrapSourceUrl", source)
        self.assertIn("data-source-url", source)
        self.assertIn("citationMarkerCount", source)
        self.assertIn("current.nextElementSibling", source)
        self.assertIn("assistant|agent|answer|markdown|ai-entry", source)
        self.assertIn("questionLinked", source)
        self.assertIn("declaredSourceCount", source)
        self.assertIn("conversationTail", source)
        self.assertIn("busySelectors", source)
        self.assertNotIn("/正在生成|思考中|搜索中|停止生成", source)
        self.assertIn("element.contains(activeInput)", source)
        self.assertNotIn('element.closest("form,[data-testid=\'chat_input\']', source)
        self.assertIn("root.querySelectorAll", source)

    def test_question_input_and_send_are_selected_and_verified(self):
        source = (EXTENSION / "content.js").read_text(encoding="utf-8")
        self.assertIn("textarea[data-testid='chat_input_input']", source)
        self.assertIn("button[data-testid='chat_input_send_button']", source)
        self.assertIn("#chat-input-box:not([disabled])", source)
        self.assertIn(".cs-input-ds-send-btn", source)
        self.assertIn("inputScore", source)
        self.assertIn("sendButtonScore", source)
        self.assertIn("submitQuestion", source)
        self.assertIn("发送动作未生效", source)
        self.assertLess(source.index("await submitQuestion"), source.index("return waitForAnswer(question, baseline"))

    def test_questions_run_silently_without_foreground_or_debugger_control(self):
        content = (EXTENSION / "content.js").read_text(encoding="utf-8")
        worker = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("GEO_PREPARE_TRUSTED_QUESTION", content)
        self.assertIn("GEO_VERIFY_TRUSTED_INPUT", content)
        self.assertIn("GEO_GET_TRUSTED_SEND_TARGET", content)
        self.assertIn("GEO_VERIFY_TRUSTED_SUBMISSION", content)
        self.assertIn("GEO_FALLBACK_SUBMIT", content)
        self.assertIn("form.requestSubmit", content)
        self.assertLess(content.index("const button = findSendButton(input)", content.index("function trustedSendTarget")), content.index("const composer =", content.index("function trustedSendTarget")))
        self.assertIn("GEO_WAIT_FOR_ANSWER", content)
        self.assertIn("GEO_CAPTURE_SNAPSHOT", content)
        self.assertIn("GEO_FILL_QUESTION_FALLBACK", content)
        self.assertIn("verificationChallenge", content)
        self.assertIn("GEO_CHALLENGE_REQUIRED", content)
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("debugger", manifest["permissions"])
        self.assertIn("runQuestionSilently", worker)
        run_model = worker[worker.index("async function runModel"):worker.index("async function runTask")]
        run_task = worker[worker.index("async function runTask"):worker.index("async function poll")]
        self.assertIn("runQuestionSilently(", run_model)
        self.assertNotIn("runQuestionWithTrustedInput(", run_model)
        self.assertNotIn("chrome.tabs.update", run_model)
        self.assertNotIn("chrome.windows", run_task)
        self.assertNotIn("dedicateModelTabs", run_task)
        self.assertIn("validAnswerBody", worker)
        self.assertIn("未识别到有效回答正文", worker)
        self.assertIn("source_capture_origins", worker)
        self.assertIn('type: "GEO_RUN_QUESTION"', worker)
        self.assertIn("reloadAfterChallenge", worker)
        self.assertIn("isVerificationChallenge", worker)
        self.assertIn("await chrome.tabs.reload(tab.id, { bypassCache: true })", worker)
        self.assertIn("if (ready?.ok) return true", worker)
        self.assertIn("刷新后验证仍存在，准备再次刷新并重试本轮", worker)
        self.assertGreaterEqual(content.count("current.challenge?.detected"), 3)
        self.assertGreaterEqual(content.count("GEO_CHALLENGE_REQUIRED"), 5)
        self.assertIn("WORKER_BUILD", worker)
        self.assertIn("后台代码 ${WORKER_BUILD}", worker)
        self.assertIn("今天能帮你做什么", worker)
        self.assertIn("下载元宝电脑版", worker)

    def test_every_round_requires_a_verified_new_conversation(self):
        content = (EXTENSION / "content.js").read_text(encoding="utf-8")
        worker = (EXTENSION / "service-worker.js").read_text(encoding="utf-8")
        self.assertIn("GEO_START_NEW_CONVERSATION", content)
        self.assertIn("GEO_GET_CONVERSATION_STATE", content)
        self.assertIn("GEO_FORCE_NEW_CONVERSATION", content)
        self.assertIn("GEO_FORCE_NEW_CONVERSATION", worker)
        self.assertIn("official-route-fallback", content)
        self.assertIn("await startFreshConversation(model)", worker)
        self.assertLess(
            worker.index("await startFreshConversation(model)"),
            worker.index("await runQuestionSilently("),
        )
        self.assertIn("[data-desc='new-chat']", content)
        self.assertIn(".new-dialog-container-button-sample", content)
        self.assertIn("location.assign(targetUrl)", content)
        self.assertIn("control instanceof HTMLAnchorElement", content)
        self.assertIn("location.assign(destination.href)", content)


if __name__ == "__main__":
    unittest.main()
