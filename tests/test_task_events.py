from datetime import datetime, timezone
import json
import unittest
from uuid import uuid4

from src.agent.responses import chat_result
from src.api_view.tasks import sse_event
from src.domain.tasks import TaskEvent
from src.services.tasks import safe_event_data


class TaskEventTests(unittest.TestCase):
    def test_event_payload_drops_messages_arguments_and_nonfinite_values(self):
        result = safe_event_data({"message": "private message", "prompt": "private prompt", "reply": "private reply",
                                  "arguments": {"quote": "private report text"}, "tool_name": "read_vehicle_report",
                                  "total_tokens": 15, "duration_ms": float("inf"), "error_type": None})
        self.assertEqual(result, {"tool_name": "read_vehicle_report", "total_tokens": 15, "error_type": None})

    def test_result_does_not_serialize_graph_messages_or_runtime_context(self):
        result = chat_result({"reply": "完成", "messages": [object()], "context_snapshot": {"private": object()}},
                              str(uuid4()), str(uuid4()))
        self.assertNotIn("messages", result)
        self.assertNotIn("context_snapshot", result)
        self.assertEqual(json.loads(json.dumps(result))["reply"], "完成")

    def test_sse_has_replayable_id_and_one_json_data_line(self):
        event = TaskEvent(task_id=uuid4(), sequence=7, run_id=uuid4(), event="tool.end",
                           data={"tool_name": "read_vehicle_report", "status": "success"}, created_at=datetime.now(timezone.utc))
        frame = sse_event(event)
        self.assertTrue(frame.startswith("id: 7\nevent: tool.end\ndata: "))
        self.assertTrue(frame.endswith("\n\n"))
        payload = json.loads(frame.split("data: ", 1)[1])
        self.assertEqual(payload["sequence"], 7)


if __name__ == "__main__":
    unittest.main()
