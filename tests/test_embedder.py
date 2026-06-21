"""tests/test_embedder.py — embedder 모듈 단위 테스트 (실제 API 호출 없음).

모든 테스트는 monkeypatch로 genai.Client 또는 get_client를 대체하므로
GEMINI_API_KEY 없이 실행된다.
"""
from __future__ import annotations

import os
import types as builtin_types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import config


# ---------------------------------------------------------------------------
# 헬퍼: 가짜 ContentEmbedding / EmbedContentResponse
# ---------------------------------------------------------------------------

def _make_fake_embedding(dim: int = config.EMBED_DIM) -> Any:
    """values 속성에 길이 dim의 float 리스트를 가진 객체를 반환한다."""
    obj = MagicMock()
    obj.values = [0.0] * dim
    return obj


def _make_fake_response(n: int = 1, dim: int = config.EMBED_DIM) -> Any:
    """n개의 가짜 ContentEmbedding을 담은 가짜 EmbedContentResponse."""
    resp = MagicMock()
    resp.embeddings = [_make_fake_embedding(dim) for _ in range(n)]
    return resp


def _make_fake_count_tokens_response(total: int = 100) -> Any:
    resp = MagicMock()
    resp.total_tokens = total
    return resp


# ---------------------------------------------------------------------------
# 픽스처: embedder 모듈을 매 테스트마다 클린 상태로 임포트
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_client():
    """각 테스트 전후로 모듈 수준 _client 캐시를 초기화한다."""
    import ingest.embedder as embedder
    embedder._client = None
    yield
    embedder._client = None


@pytest.fixture()
def fake_client():
    """embed_content / count_tokens 를 가짜 구현으로 교체한 클라이언트."""
    client = MagicMock()
    client.models.embed_content = MagicMock(return_value=_make_fake_response(1))
    client.models.count_tokens = MagicMock(
        return_value=_make_fake_count_tokens_response(50)
    )
    return client


# ---------------------------------------------------------------------------
# 1. 프리픽스 검증
# ---------------------------------------------------------------------------

class TestPrefixes:
    """embed_documents / embed_query 가 올바른 task prefix를 붙이는지 확인."""

    def test_embed_documents_uses_doc_prefix(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        embedder.embed_documents(["발명 설명 텍스트"])

        call_args = fake_client.models.embed_content.call_args
        contents = call_args.kwargs["contents"]
        assert isinstance(contents, list)
        assert contents[0].startswith(config.DOC_TASK_PREFIX), (
            f"contents[0]이 DOC_TASK_PREFIX로 시작해야 함. 실제: {contents[0][:80]!r}"
        )

    def test_embed_query_uses_query_prefix(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        # embed_query는 단일 str 반환값 1개
        fake_client.models.embed_content.return_value = _make_fake_response(1)
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        embedder.embed_query("공기압 발전 아이디어")

        call_args = fake_client.models.embed_content.call_args
        contents = call_args.kwargs["contents"]
        assert contents.startswith(config.QUERY_TASK_PREFIX), (
            f"contents가 QUERY_TASK_PREFIX로 시작해야 함. 실제: {contents[:80]!r}"
        )

    def test_embed_documents_prefix_contains_original_text(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        original = "원본 텍스트 내용"
        embedder.embed_documents([original])

        contents = fake_client.models.embed_content.call_args.kwargs["contents"]
        assert original in contents[0]

    def test_embed_query_prefix_contains_original_text(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        fake_client.models.embed_content.return_value = _make_fake_response(1)
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        original = "질의 텍스트"
        embedder.embed_query(original)

        contents = fake_client.models.embed_content.call_args.kwargs["contents"]
        assert original in contents


# ---------------------------------------------------------------------------
# 2. 반환값 형태 검증
# ---------------------------------------------------------------------------

class TestReturnShapes:
    def test_embed_documents_returns_list_of_vectors(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        n = 3
        fake_client.models.embed_content.return_value = _make_fake_response(n)
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        result = embedder.embed_documents(["a", "b", "c"])
        assert len(result) == n
        for vec in result:
            assert len(vec) == config.EMBED_DIM

    def test_embed_query_returns_single_vector(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        fake_client.models.embed_content.return_value = _make_fake_response(1)
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        result = embedder.embed_query("질의")
        assert isinstance(result, list)
        assert len(result) == config.EMBED_DIM

    def test_embed_documents_empty_input_returns_empty(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        result = embedder.embed_documents([])
        assert result == []
        fake_client.models.embed_content.assert_not_called()


# ---------------------------------------------------------------------------
# 3. 배치 처리 검증
# ---------------------------------------------------------------------------

class TestBatching:
    def test_batching_splits_into_correct_number_of_calls(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        # EMBED_BATCH_SIZE=100 → 250개 텍스트는 3배치 (100+100+50)
        n_texts = 250
        batch_size = config.EMBED_BATCH_SIZE
        expected_calls = (n_texts + batch_size - 1) // batch_size  # ceil division

        # 각 배치 호출마다 배치 크기에 맞는 응답 반환
        def side_effect(*args, **kwargs):
            contents = kwargs["contents"]
            return _make_fake_response(len(contents))

        fake_client.models.embed_content.side_effect = side_effect

        result = embedder.embed_documents(["text"] * n_texts)

        assert fake_client.models.embed_content.call_count == expected_calls
        assert len(result) == n_texts

    def test_batching_with_small_batch_size(self, fake_client, monkeypatch):
        """EMBED_BATCH_SIZE를 3으로 패치해 배치 분할·순서 보존을 검증한다."""
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)
        monkeypatch.setattr(config, "EMBED_BATCH_SIZE", 3)

        texts = ["a", "b", "c", "d", "e"]  # 5개 → 2배치 (3+2)

        call_order: list[list[str]] = []

        def side_effect(*args, **kwargs):
            batch = kwargs["contents"]
            call_order.append(batch)
            return _make_fake_response(len(batch))

        fake_client.models.embed_content.side_effect = side_effect

        result = embedder.embed_documents(texts)

        assert fake_client.models.embed_content.call_count == 2
        assert len(result) == 5

        # 배치가 순서대로 분할됐는지 확인
        assert len(call_order[0]) == 3
        assert len(call_order[1]) == 2

    def test_order_preserved_across_batches(self, fake_client, monkeypatch):
        """각 배치의 벡터가 입력 순서대로 누적되는지 확인한다."""
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)
        monkeypatch.setattr(config, "EMBED_BATCH_SIZE", 2)

        # 전역 단조 카운터로 각 벡터에 고유 식별값 부여
        global_counter = [0]

        def side_effect(*args, **kwargs):
            n = len(kwargs["contents"])
            resp = MagicMock()
            resp.embeddings = []
            for _ in range(n):
                emb = MagicMock()
                # 첫 번째 값에 전역 순서 번호를 저장 (0, 1, 2, 3, ...)
                emb.values = [float(global_counter[0])] + [0.0] * (config.EMBED_DIM - 1)
                resp.embeddings.append(emb)
                global_counter[0] += 1
            return resp

        fake_client.models.embed_content.side_effect = side_effect

        result = embedder.embed_documents(["t0", "t1", "t2", "t3"])

        assert len(result) == 4
        # 순서 보존: result[i][0]는 정확히 float(i) 이어야 함
        for i, vec in enumerate(result):
            assert vec[0] == float(i), f"result[{i}][0] = {vec[0]}, expected {float(i)}"


# ---------------------------------------------------------------------------
# 4. API 키 부재 시 RuntimeError
# ---------------------------------------------------------------------------

class TestApiKeyMissing:
    def test_get_client_raises_without_key(self, monkeypatch):
        import ingest.embedder as embedder
        # 환경변수에서 키 제거
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        embedder._client = None  # 캐시 초기화

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            embedder.get_client()

    def test_embed_documents_raises_without_key(self, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        embedder._client = None

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            embedder.embed_documents(["텍스트"])

    def test_embed_query_raises_without_key(self, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        embedder._client = None

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            embedder.embed_query("질의")

    def test_get_client_caches_after_key_set(self, monkeypatch):
        """키 설정 후 get_client()가 동일 객체를 반환하는지 확인."""
        import ingest.embedder as embedder
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-abc")
        embedder._client = None

        with patch("ingest.embedder.genai.Client") as MockClient:
            MockClient.return_value = MagicMock()
            c1 = embedder.get_client()
            c2 = embedder.get_client()
            assert c1 is c2
            MockClient.assert_called_once()


# ---------------------------------------------------------------------------
# 5. count_tokens 휴리스틱 폴백
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_count_tokens_uses_api_when_available(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        result = embedder.count_tokens("안녕하세요")
        assert result == 50  # fake_client가 50 반환

    def test_count_tokens_falls_back_on_exception(self, monkeypatch):
        """API 호출이 실패하면 문자 수 휴리스틱으로 폴백한다."""
        import ingest.embedder as embedder

        bad_client = MagicMock()
        bad_client.models.count_tokens.side_effect = Exception("API 오류")
        monkeypatch.setattr(embedder, "get_client", lambda: bad_client)

        text = "안녕하세요 발명품"  # len = 9
        result = embedder.count_tokens(text)
        expected = int(len(text) / 1.2) + 1
        assert result == expected

    def test_count_tokens_fallback_formula(self):
        """폴백 공식 int(len(text)/1.2)+1의 정확성을 직접 검증한다."""
        for text in ["", "a", "가나다라마바사아자차", "x" * 1000]:
            expected = int(len(text) / 1.2) + 1
            assert expected >= 1  # 항상 최소 1

    def test_count_tokens_fallback_on_key_error(self, monkeypatch):
        """API 키가 없어 get_client()가 RuntimeError를 던져도 폴백한다."""
        import ingest.embedder as embedder
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        embedder._client = None

        text = "테스트 텍스트"
        result = embedder.count_tokens(text)
        expected = int(len(text) / 1.2) + 1
        assert result == expected


# ---------------------------------------------------------------------------
# 6. output_dimensionality 설정 검증
# ---------------------------------------------------------------------------

class TestEmbedConfig:
    def test_embed_documents_sets_output_dimensionality(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        embedder.embed_documents(["텍스트"])

        call_kwargs = fake_client.models.embed_content.call_args.kwargs
        cfg = call_kwargs["config"]
        assert cfg.output_dimensionality == config.EMBED_DIM

    def test_embed_query_sets_output_dimensionality(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        fake_client.models.embed_content.return_value = _make_fake_response(1)
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        embedder.embed_query("질의")

        call_kwargs = fake_client.models.embed_content.call_args.kwargs
        cfg = call_kwargs["config"]
        assert cfg.output_dimensionality == config.EMBED_DIM

    def test_embed_documents_uses_correct_model(self, fake_client, monkeypatch):
        import ingest.embedder as embedder
        monkeypatch.setattr(embedder, "get_client", lambda: fake_client)

        embedder.embed_documents(["텍스트"])

        call_kwargs = fake_client.models.embed_content.call_args.kwargs
        assert call_kwargs["model"] == config.EMBED_MODEL
