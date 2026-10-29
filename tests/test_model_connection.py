import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import APIConnectionError, APITimeoutError
from langchain_openai.chat_models.base import OpenAIConnectionError

from src.agent.config import get_settings
from src.agent.models import create_model
from src.api_view.runs import run_error
from src.api_view.app import app
from src.services.tasks import error_details
from tests.support import install_test_identity


class ModelConnectionTests(unittest.TestCase):
    def test_sync_and_async_clients_obey_project_proxy_setting(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                settings = get_settings().model_copy(update={"model_use_env_proxy": enabled})
                model = create_model(settings)
                try:
                    self.assertEqual(model.http_client._trust_env, enabled)
                    self.assertEqual(model.http_async_client._trust_env, enabled)
                    self.assertIs(model.root_client._client, model.http_client)
                    self.assertIs(model.root_async_client._client, model.http_async_client)
                    self.assertEqual(model.http_socket_options, ())
                    self.assertEqual(model.max_retries, 0)
                finally:
                    model.http_client.close()
                    asyncio.run(model.http_async_client.aclose())

    def test_model_connection_errors_are_specific_and_sanitized(self):
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        for error in (APIConnectionError(message="private connection detail", request=request),
                      OpenAIConnectionError(message="private connection detail", request=request)):
            with self.subTest(error=type(error).__name__):
                status, code, message = error_details(error)
                self.assertEqual((status, code), ("failed", "model_connection_failed"))
                self.assertIn("CHEXU_MODEL_USE_ENV_PROXY", message)
                response = run_error(error)
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.detail["code"], code)
                self.assertNotIn("private connection detail", str(response.detail))
                self.assertNotIn("provider.invalid", str(response.detail))

    def test_timeout_is_not_misclassified_as_connection_failure(self):
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        error = APITimeoutError(request=request)
        self.assertEqual(error_details(error)[:2], ("timed_out", "model_request_timed_out"))
        self.assertEqual(run_error(error).status_code, 504)

    def test_chat_endpoint_explains_connection_failure_without_exposing_details(self):
        install_test_identity(self)
        error = OpenAIConnectionError(message="private provider error", request=httpx.Request("POST", "https://provider.invalid/"))
        client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(client.close)
        with patch("src.api_view.app.run_chat", side_effect=error):
            response = client.post("/api/chat", json={"message": "联网查车型资料"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["code"], "model_connection_failed")
        self.assertIn("CHEXU_MODEL_USE_ENV_PROXY", response.json()["detail"]["message"])
        self.assertNotIn("private provider error", response.text)
