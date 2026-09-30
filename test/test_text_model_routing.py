from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.account_service import AccountService
from services.model_service import ModelRoute, ModelUnavailableError, model_account_key
from services.protocol import (
    anthropic_v1_messages,
    conversation,
    openai_v1_chat_complete,
    openai_v1_response,
)
from services.protocol.conversation import TextCompletionOutput
from services.storage.json_storage import JSONStorageBackend


class TextAccountRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = AccountService(
            JSONStorageBackend(Path(self.temp_dir.name) / "accounts.json")
        )
        self.service.add_account_items(
            [
                {"access_token": "free", "type": "free", "status": "正常"},
                {"access_token": "plus", "type": "Plus", "status": "正常"},
                {"access_token": "pro", "type": "Pro", "status": "正常"},
                {"access_token": "pro-disabled", "type": "Pro", "status": "禁用"},
            ]
        )
        self.service.refresh_access_token = lambda token, **_kwargs: token

    def test_explicit_model_selects_only_advertising_account_type(self) -> None:
        route = ModelRoute(account_types=frozenset({"Pro"}), allow_anonymous=False)
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ):
            token = self.service.get_text_access_token(model="pro-only")

        self.assertEqual(token, "pro")

    def test_selected_model_rotates_only_across_eligible_accounts(self) -> None:
        route = ModelRoute(
            account_types=frozenset({"free", "Plus", "Pro"}),
            allow_anonymous=False,
            upstream_model="gpt-6.1-sol",
        )
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ):
            tokens = {
                self.service.get_text_access_token(model="gpt-6.1-sol"),
                self.service.get_text_access_token(model="gpt-6.1-sol"),
                self.service.get_text_access_token(model="gpt-6.1-sol"),
            }

        self.assertEqual(tokens, {"free", "plus", "pro"})

    def test_anonymous_model_uses_anonymous_backend(self) -> None:
        route = ModelRoute(account_types=frozenset(), allow_anonymous=True)
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ):
            token = self.service.get_text_access_token(model="anon-only")

        self.assertEqual(token, "")

    def test_model_without_eligible_account_fails_closed(self) -> None:
        route = ModelRoute(account_types=frozenset({"Team"}), allow_anonymous=False)
        with mock.patch(
            "services.model_service.model_catalog_service.route_for_model",
            return_value=route,
        ), self.assertRaisesRegex(ModelUnavailableError, "team-only"):
            self.service.get_text_access_token(model="team-only")


class TextProtocolRoutingTests(unittest.TestCase):
    def test_account_selector_respects_individual_permissions(self) -> None:
        route = ModelRoute(
            account_types=frozenset({"Pro"}),
            account_keys=frozenset({model_account_key("pro")}),
        )
        with tempfile.TemporaryDirectory() as directory:
            service = AccountService(JSONStorageBackend(Path(directory) / "accounts.json"))
            service.add_account_items([
                {"access_token": "pro-old", "type": "Pro"},
                {"access_token": "pro", "type": "Pro"},
            ])
            service.refresh_access_token = lambda token, **_kwargs: token
            with mock.patch("services.model_service.model_catalog_service.route_for_model", return_value=route):
                for _ in range(3):
                    self.assertEqual(service.get_text_access_token(model="gpt-6.1-sol"), "pro")
                with self.assertRaises(ModelUnavailableError):
                    service.get_text_access_token(model="gpt-6.1-sol", excluded_tokens={"pro"})

    def test_text_backend_passes_requested_model_to_account_selector(self) -> None:
        backend = mock.Mock()
        with (
            mock.patch.object(
                conversation.account_service,
                "get_text_access_token",
                return_value="pro",
            ) as selector,
            mock.patch.object(conversation, "OpenAIBackendAPI", return_value=backend),
        ):
            result = conversation.text_backend("pro-only")

        self.assertIs(result, backend)
        selector.assert_called_once_with(model="pro-only")

    def test_chat_completions_passes_requested_model_to_text_backend(self) -> None:
        body = {
            "model": "gpt-6.1-sol",
            "messages": [{"role": "user", "content": "route chat"}],
        }
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()) as backend,
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content="ok"),
            ),
        ):
            openai_v1_chat_complete.handle(body)

        backend.assert_called_once_with("gpt-6.1-sol")

    def test_conversation_bridges_codex_client_model_to_web_slug(self) -> None:
        backend = mock.Mock()
        backend.stream_conversation.return_value = iter(())
        with mock.patch(
            "services.model_service.model_catalog_service.resolve_model",
            return_value="gpt-6-sol-wm",
        ) as resolver:
            list(conversation.conversation_events(
                backend,
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-6-sol",
            ))

        resolver.assert_called_once_with("gpt-6-sol")
        self.assertEqual(
            backend.stream_conversation.call_args.kwargs["model"],
            "gpt-6-sol-wm",
        )
        injected = backend.stream_conversation.call_args.kwargs["messages"]
        self.assertEqual(injected, [{"role": "user", "content": "hello"}])

    def test_conversation_does_not_inject_a_claimed_model_identity(self) -> None:
        backend = mock.Mock()
        backend.stream_conversation.return_value = iter(())
        with mock.patch(
            "services.model_service.model_catalog_service.resolve_model",
            return_value="gpt-6-sol-wm",
        ):
            list(conversation.conversation_events(
                backend,
                messages=[{"role": "user", "content": "你是什么模型"}],
                model="gpt-6-sol-wm",
            ))

        injected = backend.stream_conversation.call_args.kwargs["messages"]
        self.assertEqual(
            backend.stream_conversation.call_args.kwargs["model"],
            "gpt-6-sol-wm",
        )
        self.assertEqual(injected, [{"role": "user", "content": "你是什么模型"}])

    def test_chat_pro_is_forwarded_without_work_mode_suffix(self) -> None:
        backend = mock.Mock()
        backend.stream_conversation.return_value = iter(())
        with mock.patch(
            "services.model_service.model_catalog_service.resolve_model", return_value="gpt-6-pro",
        ) as resolver:
            list(conversation.conversation_events(
                backend, messages=[{"role": "user", "content": "hello"}], model="gpt-6-pro",
            ))
        resolver.assert_called_once_with("gpt-6-pro")
        self.assertEqual(backend.stream_conversation.call_args.kwargs["model"], "gpt-6-pro")

    def test_responses_passes_requested_model_to_text_backend(self) -> None:
        body = {"model": "gpt-6.1-sol", "input": "route response"}
        with (
            mock.patch.object(openai_v1_response, "text_backend", return_value=object()) as backend,
            mock.patch.object(openai_v1_response, "stream_text_deltas", return_value=iter(["ok"])),
        ):
            openai_v1_response.handle(body)

        backend.assert_called_once_with("gpt-6.1-sol")

    def test_anthropic_messages_passes_requested_model_to_account_selector(self) -> None:
        with (
            mock.patch.object(
                anthropic_v1_messages.account_service,
                "get_text_access_token",
                return_value="pro",
            ) as selector,
            mock.patch.object(anthropic_v1_messages, "OpenAIBackendAPI"),
        ):
            request = anthropic_v1_messages.message_request({
                "model": "gpt-6.1-sol",
                "messages": [{"role": "user", "content": "route anthropic"}],
            })

        self.assertEqual(request.model, "gpt-6.1-sol")
        selector.assert_called_once_with(model="gpt-6.1-sol")

    def test_invalid_token_retry_keeps_requested_model_filter(self) -> None:
        initial_backend = SimpleNamespace(access_token="bad")
        request = conversation.ConversationRequest(
            model="pro-only",
            messages=[{"role": "user", "content": "hello"}],
        )

        def fake_events(backend, **_kwargs):
            if backend.access_token == "bad":
                raise RuntimeError("token_invalidated")
            yield {"type": "conversation.delta", "delta": "ok"}

        with (
            mock.patch.object(conversation, "OpenAIBackendAPI", side_effect=lambda access_token: SimpleNamespace(
                access_token=access_token,
                close=lambda: None,
            )),
            mock.patch.object(conversation, "conversation_events", side_effect=fake_events),
            mock.patch.object(
                conversation.account_service,
                "refresh_access_token",
                return_value="bad",
            ),
            mock.patch.object(conversation.account_service, "remove_invalid_token"),
            mock.patch.object(
                conversation.account_service,
                "get_text_access_token",
                return_value="pro",
            ) as selector,
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            result = list(conversation.stream_text_deltas(initial_backend, request))

        self.assertEqual(result, ["ok"])
        selector.assert_called_once_with(
            excluded_tokens={"bad"},
            model="pro-only",
        )


if __name__ == "__main__":
    unittest.main()
